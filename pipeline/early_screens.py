#!/usr/bin/env python3
"""Parse phone screenshots and preserve a clean early leaderboard prefix."""
import statistics
import unicodedata
from collections import Counter

from frame_parser import parse_frame
from merge import ObservationStore, name_key as player_name_key

# Header centres in the game's full-width portrait layout, independent of the phone.
_HEADER_X = {"RANKINGS": 0.1434, "Rank": 0.1008, "Commander": 0.4690, "Points": 0.8540}
_TITLE_Y = 0.9760
_COLUMN_Y = 0.8888


def _fit_axis(expected, observed):
    mean_x, mean_y = statistics.mean(expected), statistics.mean(observed)
    variance = sum((x - mean_x) ** 2 for x in expected)
    scale = sum((x - mean_x) * (y - mean_y) for x, y in zip(expected, observed)) / variance
    return mean_y - scale * mean_x, scale


def remap_to_game_column(items):
    """Fit the game rectangle to the Rankings headers and return copied OCR boxes.

    OCR coordinates have a bottom-left origin. The title and column-header heights
    also reveal vertical letterboxing; ordinary full-height screenshots keep their y.
    """
    headers = {}
    for item in items:
        text = item["text"].strip()
        if text in _HEADER_X:
            headers[text] = item
    if not {"RANKINGS", "Rank", "Commander", "Points"}.issubset(headers):
        raise ValueError("Early screenshot is missing the Rankings column headers")

    names = list(_HEADER_X)
    left, width = _fit_axis([_HEADER_X[n] for n in names],
                            [headers[n]["x"] + headers[n]["width"] / 2 for n in names])
    title = headers["RANKINGS"]
    title_y = title["y"] + title["height"] / 2
    column_y = statistics.median(headers[n]["y"] + headers[n]["height"] / 2
                                 for n in ("Rank", "Commander", "Points"))
    bottom, height = _fit_axis([_COLUMN_Y, _TITLE_Y], [column_y, title_y])
    if width <= 0 or height <= 0:
        raise ValueError("Early screenshot has invalid Rankings header geometry")
    if abs(bottom) < 0.01 and abs(height - 1) < 0.01:
        bottom, height = 0.0, 1.0

    remapped = []
    for item in items:
        box = dict(item)
        box.update(x=(item["x"] - left) / width, width=item["width"] / width,
                   y=(item["y"] - bottom) / height, height=item["height"] / height)
        # Ignore OCR from phone chrome or text outside the game rectangle.
        if 0 <= box["x"] + box["width"] / 2 <= 1 and 0 <= box["y"] + box["height"] / 2 <= 1:
            remapped.append(box)
    return remapped


def parse_early_screens(list_of_items_lists):
    """Parse and vote all early screenshots, retaining per-rank diagnostics."""
    store = ObservationStore()
    for index, items in enumerate(list_of_items_lists, 1):
        store.add_frame(f"early_{index:03d}", parse_frame(remap_to_game_column(items)))
    return store.merge_all()


def _edit_distance(a, b, limit=2):
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (char_a != char_b)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _alliance_key(row):
    return "".join(unicodedata.normalize("NFKC", row.get("alliance") or "").split()).casefold()


def _server(row):
    return str(row.get("server") or "").lstrip("Ss")


def _match_players(early_rows, capture_rows, key):
    """Match exact keys first; permit unique nearby keys with agreeing identity fields."""
    by_key = {}
    for index, row in enumerate(capture_rows):
        by_key.setdefault(key(row.get("commander", "")), []).append(index)
    matches, used = {}, set()
    home_votes = Counter()
    for early_index, row in enumerate(early_rows):
        candidates = by_key.get(key(row.get("commander", "")), [])
        available = [i for i in candidates if i not in used]
        if available:
            index = min(available, key=lambda i: (_server(row) != _server(capture_rows[i]),
                                                  _alliance_key(row) != _alliance_key(capture_rows[i]), i))
            matches[early_index] = index
            used.add(index)
            if not _server(row):
                home_votes[_server(capture_rows[index])] += 1
    # A missing server line denotes home; infer its number from exact matches.
    home = home_votes.most_common(1)[0][0] if home_votes else "117"
    for early_index, row in enumerate(early_rows):
        observed = key(row.get("commander", ""))
        if early_index in matches or not observed or observed in by_key:
            continue
        candidates = [i for i, capture in enumerate(capture_rows)
                      if i not in used and (_server(row) or home) == (_server(capture) or home)
                      and _alliance_key(row) == _alliance_key(capture)
                      and _edit_distance(observed, key(capture.get("commander", ""))) <= 2]
        if len(candidates) == 1:
            matches[early_index] = candidates[0]
            used.add(candidates[0])
    return matches


def max_clean_cutoff(early_rows, capture_rows, name_key=player_name_key):
    """Largest contiguous early prefix whose outsiders stayed below its last score."""
    early = sorted(early_rows, key=lambda r: r["rank"])
    matches = _match_players(early, capture_rows, name_key)
    included, cutoff = set(), 0
    for index, row in enumerate(early):
        if row["rank"] != index + 1 or row.get("points") is None:
            break
        if index in matches:
            included.add(matches[index])
        if all(i in included or capture.get("points") is None or capture["points"] <= row["points"]
               for i, capture in enumerate(capture_rows)):
            cutoff = row["rank"]
    return cutoff


def merge_early(early_rows, capture_rows, cutoff, time_range=None):
    """Keep early prefix scores/order, use capture identities, then append capture order.

    Returns (records, provenance); time_range is optional capture metadata supplied
    by the caller, already converted from the image filenames or file timestamps.
    """
    early = sorted(early_rows, key=lambda r: r["rank"])
    if cutoff < 0 or [r["rank"] for r in early[:cutoff]] != list(range(1, cutoff + 1)):
        raise ValueError("Early cutoff must select contiguous ranks starting at 1")
    matches = _match_players(early, capture_rows, player_name_key)
    selected = set()
    records = []
    for index, row in enumerate(early[:cutoff]):
        if index not in matches:
            raise ValueError(f"Early rank {row['rank']} ({row.get('commander', '')}) has no capture match")
        capture_index = matches[index]
        selected.add(capture_index)
        merged = dict(capture_rows[capture_index])
        merged.update(rank=row["rank"], points=row["points"])
        if "diagnostics" in merged:
            merged["diagnostics"] = dict(row.get("diagnostics", merged["diagnostics"]), rank=row["rank"])
        records.append(merged)

    passed = []
    cutoff_score = early[cutoff - 1]["points"] if cutoff else None
    for index, row in enumerate(capture_rows):
        if index in selected:
            continue
        merged = dict(row, rank=len(records) + 1)
        if "diagnostics" in merged:
            merged["diagnostics"] = dict(merged["diagnostics"], rank=merged["rank"])
        records.append(merged)
        if cutoff_score is not None and row.get("points") is not None and row["points"] > cutoff_score:
            passed.append({"commander": row["commander"], "server": row.get("server"),
                           "rank": merged["rank"], "capture_rank": row["rank"], "points": row["points"]})
    provenance = {"cutoff": cutoff, "passed_cutoff_score": passed}
    if time_range is not None:
        provenance["early_time_range"] = time_range
    return records, provenance
