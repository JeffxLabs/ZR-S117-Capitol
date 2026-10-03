#!/usr/bin/env python3
"""
Multi-Observation Merger & Voting System for Z Route: Redemption Capitol War.

Aggregates multiple OCR observations per rank across video/swipe frames,
applies robust majority voting filtered by cleanliness and completeness,
and produces per-rank diagnostic metrics.
"""
from collections import Counter
from typing import Dict, List, Optional, Any, Tuple


class ObservationStore:
    """Stores all raw frame observations and merges them by rank."""

    def __init__(self):
        # Map rank -> list of observation dicts
        self.observations_by_rank: Dict[int, List[Dict[str, Any]]] = {}
        # List of frame-level records for raw_observations.json
        self.frames_records: List[Dict[str, Any]] = []

    def add_frame(
        self,
        frame_name: str,
        frame_result: Any,
    ):
        """Add all rows from a FrameResult."""
        rows_data = []
        for r in frame_result.rows:
            obs = dict(r)
            obs["frame"] = frame_name
            rows_data.append(obs)
            rk = r["rank"]
            self.observations_by_rank.setdefault(rk, []).append(obs)

        self.frames_records.append({
            "frame": frame_name,
            "banner_detected": getattr(frame_result, "banner_detected", False),
            "banner_band": getattr(frame_result, "banner_band", None),
            "offset": getattr(frame_result, "offset", None),
            "rows": rows_data
        })

    def add_observation(self, obs: Dict[str, Any], frame_name: Optional[str] = None):
        """Add a single observation dict."""
        o = dict(obs)
        if frame_name:
            o["frame"] = frame_name
            existing = next((f for f in self.frames_records if f["frame"] == frame_name), None)
            if existing:
                existing["rows"].append(o)
            else:
                self.frames_records.append({
                    "frame": frame_name,
                    "rows": [o]
                })
        rk = o["rank"]
        self.observations_by_rank.setdefault(rk, []).append(o)

    def get_raw_observations(self) -> List[Dict[str, Any]]:
        """Return frame records for raw_observations.json."""
        return self.frames_records

    def merge_all(self) -> List[Dict[str, Any]]:
        """Merge all stored observations across all ranks sorted by rank."""
        sorted_ranks = sorted(self.observations_by_rank.keys())
        return [merge_rank(r, self.observations_by_rank[r]) for r in sorted_ranks]


def _vote_field(
    pool: List[Dict[str, Any]],
    field: str,
    allow_empty: bool = False
) -> Tuple[Any, float]:
    """Perform majority voting for a field with tie-breaking nearest screen centre."""
    if allow_empty:
        valid_obs = [o for o in pool if field in o and o[field] is not None]
    else:
        valid_obs = [o for o in pool if o.get(field) is not None and o.get(field) != ""]

    if not valid_obs:
        return (None if field in ("points", "server") else ""), 0.0

    vals = [o[field] for o in valid_obs]
    counts = Counter(vals)
    max_count = max(counts.values())
    candidates = [v for v, c in counts.items() if c == max_count]

    if len(candidates) == 1:
        winner = candidates[0]
    else:
        # Tie-breaker: select the candidate whose observation is closest to screen centre (y = 0.45)
        cand_obs = [o for o in valid_obs if o[field] in candidates]
        cand_obs.sort(key=lambda o: abs(o.get("y", 0.45) - 0.45))
        winner = cand_obs[0][field]

    agreement = counts[winner] / len(valid_obs)
    return winner, agreement


def merge_rank(rank: int, observations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge all observations for a single rank using majority voting.

    Clean, complete observations always take precedence over tainted or incomplete ones.
    Tainted observations are only considered if no clean complete observations exist.
    """
    n_obs = len(observations)
    clean_complete = [
        o for o in observations
        if o.get("complete") and not o.get("tainted")
    ]
    n_clean = len(clean_complete)
    used_tainted = False

    if n_clean > 0:
        pool = clean_complete
    else:
        # Fall back to tainted complete observations
        tainted_complete = [
            o for o in observations
            if o.get("complete") and o.get("tainted")
        ]
        if tainted_complete:
            pool = tainted_complete
            used_tainted = True
        elif observations:
            # Fall back to any available observation
            pool = observations
            used_tainted = any(o.get("tainted", False) for o in observations)
        else:
            pool = []

    best_points, points_agreement = _vote_field(pool, "points")
    best_cmd, cmd_agreement = _vote_field(pool, "commander")
    best_ally, ally_agreement = _vote_field(pool, "alliance")
    # Server: an observation without a server line is a vote for the home server (None),
    # so one stray "S113" cannot outvote several clean home-server reads.
    server_votes = Counter(o.get("server") for o in pool)
    best_server = server_votes.most_common(1)[0][0] if server_votes else None

    diagnostics = {
        "rank": rank,
        "n_obs": n_obs,
        "n_clean": n_clean,
        "used_tainted": used_tainted,
        "agreement": {
            "points": round(points_agreement, 3),
            "commander": round(cmd_agreement, 3),
            "alliance": round(ally_agreement, 3),
        }
    }

    return {
        "rank": rank,
        "commander": best_cmd,
        "alliance": best_ally,
        "server": best_server,
        "points": best_points,
        "diagnostics": diagnostics,
    }
