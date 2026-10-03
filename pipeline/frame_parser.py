#!/usr/bin/env python3
"""
Leaderboard Frame Parser for Z Route: Redemption Capitol War.

Extracts structured player rows from raw Apple Vision OCR item lists using
geometric slot clustering, median rank offset fitting, and notification banner detection.
"""
import re
import statistics
from typing import List, Dict, Optional, Tuple, Any

# Geometric layout constants (normalized coordinates, 0-1, origin bottom-left)
PITCH = 0.0927           # Row pitch (~178 px on 1920h)
HEADER_Y = 0.865        # Screen header cutoff
FOOTER_Y = 0.050        # Screen footer cutoff
EDGE_TOP_Y = 0.830      # Slot centers above this can have the commander line cut off by the header
EDGE_BOT_Y = 0.080      # Slot centers below this have server/alliance cut off by footer

# Banner detection band constants
BANNER_BAND_MIN = 0.72
BANNER_BAND_MAX = 0.84
BANNER_OVERLAY_MIN = 0.74
BANNER_OVERLAY_MAX = 0.81


class Row(dict):
    """Represents a parsed leaderboard row slot."""
    def __init__(
        self,
        rank: int,
        commander: str,
        alliance: str,
        server: Optional[str],
        points: Optional[int],
        y: float,
        tainted: bool,
        complete: bool
    ):
        super().__init__(
            rank=rank,
            commander=commander,
            alliance=alliance,
            server=server,
            points=points,
            y=round(y, 4),
            tainted=tainted,
            complete=complete
        )

    @property
    def rank(self) -> int:
        return self["rank"]

    @property
    def commander(self) -> str:
        return self["commander"]

    @property
    def alliance(self) -> str:
        return self["alliance"]

    @property
    def server(self) -> Optional[str]:
        return self["server"]

    @property
    def points(self) -> Optional[int]:
        return self["points"]

    @property
    def y(self) -> float:
        return self["y"]

    @property
    def tainted(self) -> bool:
        return self["tainted"]

    @property
    def complete(self) -> bool:
        return self["complete"]


class FrameResult:
    """Result of parsing a single OCR frame."""
    def __init__(
        self,
        rows: List[Row],
        banner_detected: bool = False,
        banner_band: Optional[Tuple[float, float]] = None,
        offset: Optional[int] = None
    ):
        self.rows = rows
        self.banner_detected = banner_detected
        self.banner_band = banner_band
        self.banner = {"detected": banner_detected, "band": banner_band}
        self.offset = offset

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)


def detect_banner(items: List[Dict[str, Any]], med_h: float) -> Tuple[bool, Optional[Tuple[float, float]]]:
    """Detect whether a notification banner overlays the frame."""
    banner_boxes = []
    for it in items:
        x, y, w, h = it["x"], it["y"], it["width"], it["height"]
        text = it["text"].strip()
        if not (BANNER_BAND_MIN <= y <= BANNER_BAND_MAX):
            continue

        # Condition 1: spans column boundaries
        cond1 = (x < 0.30 and x + w > 0.40) or (x < 0.72 and x + w > 0.78)

        # Condition 2: lone short X/x/× at x > 0.80 within y 0.72-0.84
        cond2 = (text in ["X", "x", "×"] and x > 0.80)

        # Condition 3: starts left of middle column (0.12 < x < 0.32) and is not numeric rank (in banner region)
        clean_d = re.sub(r"[^\d]", "", text)
        cond3 = (0.12 < x < 0.32 and not clean_d and 0.75 <= y <= 0.80)

        # Condition 4: commander box with height < 0.6x median text height inside banner band
        cond4 = (0.32 <= x <= 0.74 and h < 0.6 * med_h and 0.75 <= y <= 0.82)

        if cond1 or cond2 or cond3 or cond4:
            banner_boxes.append(it)

    if banner_boxes:
        return True, (0.75, 0.82)
    return False, None


def parse_frame(items: List[Dict[str, Any]], prev_max_rank: Optional[int] = None) -> FrameResult:
    """Parse raw Apple Vision OCR items into structured player rows.

    Args:
        items: List of dicts with text, confidence, x, y, width, height (normalized 0-1, bottom-left origin).
        prev_max_rank: Highest rank observed so far in the pipeline, if known.

    Returns:
        FrameResult with rows, banner_detected, banner_band, and fitted offset.
    """
    # 1. Compute median text height for rows in visible area
    visible_heights = [
        it["height"] for it in items
        if FOOTER_Y < it["y"] < HEADER_Y and it["height"] > 0.005
    ]
    med_h = statistics.median(visible_heights) if visible_heights else 0.022

    # 2. Banner detection
    banner_detected, banner_band = detect_banner(items, med_h)

    # 3. Categorize boxes in the visible area (ignoring header and footer)
    anchors = []
    points_boxes = []
    middle_boxes = []

    for it in items:
        x, y, w, h = it["x"], it["y"], it["width"], it["height"]
        text = it["text"].strip()
        if y > HEADER_Y or y < FOOTER_Y:
            continue

        clean_num = re.sub(r"[^\d]", "", text)
        if x > 0.73 and clean_num:
            val = int(clean_num)
            anchors.append((y, "pts", val, it))
            points_boxes.append((y, val, it))
        elif x < 0.20 and text.isdigit():
            val = int(text)
            anchors.append((y, "rnk", val, it))
        elif 0.32 <= x <= 0.74:
            middle_boxes.append(it)

    if not anchors:
        return FrameResult(rows=[], banner_detected=banner_detected, banner_band=banner_band, offset=None)

    # 4. Cluster anchors by y to identify row slots
    anchors.sort(key=lambda a: a[0], reverse=True)
    clusters: List[List[Tuple[float, str, int, Dict[str, Any]]]] = []
    for a in anchors:
        matched = False
        for c in clusters:
            mean_y = sum(x[0] for x in c) / len(c)
            if abs(a[0] - mean_y) < 0.035:
                c.append(a)
                matched = True
                break
        if not matched:
            clusters.append([a])

    # Slot centers (sorted top to bottom)
    clusters.sort(key=lambda c: sum(x[0] for x in c) / len(c), reverse=True)
    cluster_ys = [sum(x[0] for x in c) / len(c) for c in clusters]
    top_y = cluster_ys[0]
    indices = [int(round((top_y - cy) / PITCH)) for cy in cluster_ys]

    # 5. Compute frame rank offset: median of (OCR rank - slot index)
    offsets = []
    for idx, c in zip(indices, clusters):
        rnks = [x[2] for x in c if x[1] == "rnk"]
        for r in rnks:
            candidate = r - idx
            if prev_max_rank is not None and candidate < prev_max_rank - 25:
                continue
            offsets.append(candidate)

    if offsets:
        fitted_offset = int(round(statistics.median(offsets)))
    elif prev_max_rank is None:
        fitted_offset = 1
    else:
        fitted_offset = prev_max_rank + 1

    # Infer medal rows (ranks 1-3) when frame is at the top
    if fitted_offset < 1:
        fitted_offset = 1

    # 6. Assign middle-column boxes to the nearest slot centre (within half a pitch)
    slot_middle_map: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(cluster_ys))}
    for m in middle_boxes:
        my = m["y"]
        best_i = None
        min_dist = PITCH / 2.0
        for i, cy in enumerate(cluster_ys):
            dist = abs(my - cy)
            if dist <= min_dist:
                min_dist = dist
                best_i = i
        if best_i is not None:
            slot_middle_map[best_i].append(m)

    # 7. Construct Row for each slot
    rows: List[Row] = []
    for i, (idx, cy) in enumerate(zip(indices, cluster_ys)):
        rank = fitted_offset + idx

        # Points for this slot (nearest within half pitch)
        pts_cands = [p[1] for p in points_boxes if abs(p[0] - cy) <= PITCH / 2.0]
        points_val = pts_cands[0] if pts_cands else None

        # Middle lines sorted top -> bottom (decreasing y)
        mids = sorted(slot_middle_map[i], key=lambda m: m["y"], reverse=True)
        commander = mids[0]["text"].strip() if mids else ""
        server = None
        alliance_lines = []
        for m in mids[1:]:
            t = m["text"].strip()
            sm = re.match(r"^S(\d{2,4})$", t)
            if sm and server is None:
                server = sm.group(1)
            else:
                alliance_lines.append(t)
        alliance = " ".join(alliance_lines)

        # Edge cutoff: top rows missing commander, bottom rows missing server/alliance
        is_edge_cutoff = (cy > EDGE_TOP_Y or cy < EDGE_BOT_Y)
        # First line looks like an alliance tag: at the screen edge the name line was cut off
        # (incomplete); inside the list the name is unreadable to OCR (e.g. circled letters
        # like Ⓚⓐⓣⓒⓗ), so keep the row's points/alliance/server and leave the name empty.
        name_unreadable = False
        if commander.startswith("[") and not alliance and not is_edge_cutoff:
            alliance, commander, name_unreadable = commander, "", True
        complete = bool((commander or name_unreadable) and points_val is not None and not is_edge_cutoff
                        and not (commander.startswith("[") and not alliance))

        # Banner overlap check
        tainted = False
        if banner_detected and (BANNER_OVERLAY_MIN <= cy <= BANNER_OVERLAY_MAX):
            tainted = True

        rows.append(Row(
            rank=rank,
            commander=commander,
            alliance=alliance,
            server=server,
            points=points_val,
            y=cy,
            tainted=tainted,
            complete=complete
        ))
        rows[-1]["name_unreadable"] = name_unreadable

    return FrameResult(
        rows=rows,
        banner_detected=banner_detected,
        banner_band=banner_band,
        offset=fitted_offset
    )
