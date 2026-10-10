#!/usr/bin/env python3
"""
Data Cleaning & Normalization for Z Route: Redemption Capitol War Rankings.
Resolves OCR artifacts, consolidates alliances, and validates integrity.
"""
import os
import re
from collections import Counter, defaultdict

from known_names import (apply_known_names, build_known_name_index, confusable_key,
                         event_rankings, load_overrides)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALLIANCE_NORMALIZATION_MAP = {
    "[P1MP] JU1CER": "[P1MP] JU1CE",
    "[P1MP]JU1CER": "[P1MP] JU1CE",
    "[P1MP]JU1CE": "[P1MP] JU1CE",
    "[7DS] Se7enDeàdlySinsR": "[7DS] Se7enDeàdlySins",
    "[cHIL] ChillEmpire": "[CHIL] ChillEmpire",
    "[CLAVI ВОЙНЫ": "[CLAV] ВОЙНЫ",
    "[CLAV] ВОЙНЫ-": "[CLAV] ВОЙНЫ",
    "[FBLL] DVDVDVD": "[FBL3] DVDVDVDC",
    "[HElI] DarkAl": "[HEII] DarkAl",
    "[Hhhh] Xpovoç": "[Hhhh] Xpovos",
    "[QCKJ QUICKREACTIONFORCE": "[QCK] QUICKREACTIONFORCE",
    "[RTRJ RETRO": "[RTR] RETRO",
    "TSKG] Horizon": "[SKG] Horizon",
    "UJQK] JQKKK": "[JQK] JQKKK",
    "UQK]JOKKK": "[JQK] JQKKK",
    "[19o] CandyClub": "[190] CandyClub",
    "[1BİR] XOXOX": "[1BiR] XOXOX",
    "[TІOX] ПоХуисты": "[TIOX] ПоХуисты",
    "Df] 아무이름이나붙이기": "[Df] 아무이름이나붙이기",
    "[OBS] ZeroBullsht": "[0BS] ZeroBullsht",
    "[Geujl hehrb": "[Geuj] hehrb",
    "[|HA] USAL]": "[IHA] USALJ",
    "[|HA] USALJ": "[IHA] USALJ",
    "[IHA] USAL)": "[IHA] USALJ",
    "Usib] aksjf": "[Jsib] aksjf",
    "[sib] aksjf": "[Jsib] aksjf",
    "ICCCC] CCCCCCCCCCCCCCCCCCCC": "[CCCC] CCCCCCCCCCCCCCCCCCCC",
    "USMF] forte": "[USMF] forte",
    "[zZZ] Zombiz": "[zZZ] ZombiZ"
}

_LOOKALIKE = str.maketrans({"0": "o", "O": "o", "1": "l", "I": "l", "i": "l", "|": "l", "!": "l"})

def _alliance_key(alliance):
    return re.sub(r'[\s\[\]()]', '', alliance).translate(_LOOKALIKE).lower()

def consolidate_alliance_variants(records):
    """Merge alliances that differ only by OCR look-alike characters (O/0, I/l/1/|)
    into the most frequent spelling. Returns a list of (variant, canonical, count)."""
    groups = {}
    for r in records:
        if r["alliance"] != "No Alliance":
            groups.setdefault(_alliance_key(r["alliance"]), {}).setdefault(r["alliance"], 0)
            groups[_alliance_key(r["alliance"])][r["alliance"]] += 1
    merged = []
    for variants in groups.values():
        if len(variants) < 2:
            continue
        canonical = max(variants, key=variants.get)
        for v, n in variants.items():
            if v != canonical:
                merged.append((v, canonical, n))
    lookup = {v: c for v, c, _ in merged}
    for r in records:
        if r["alliance"] in lookup:
            r["alliance"] = lookup[r["alliance"]]
            r["alliance_tag"], r["alliance_name"] = parse_tag_and_name(r["alliance"])
    return merged

def parse_tag_and_name(alliance_str):
    if not alliance_str:
        return "", "No Alliance"
    m = re.match(r'^\[(.*?)\]\s*(.*)$', alliance_str.strip())
    if m:
        return re.sub(r'\s+', '', m.group(1)), m.group(2).strip()
    return "", alliance_str.strip()

def clean_player_record(record, home_server="117", visiting_server="119", known_alliances=None):
    rank = record["rank"]
    cmd = record.get("commander", "").strip()
    raw_ally = record.get("alliance", "").strip().translate(str.maketrans("［］", "[]"))
    pts = record.get("points")

    # 1. Determine server: take explicit server if present in record (None/'' -> home)
    rec_server = record.get("server")
    if rec_server is not None and str(rec_server).strip() != "":
        srv_str = str(rec_server).strip()
        if srv_str.startswith("S") or srv_str.startswith("s"):
            srv_str = srv_str[1:]
        server = srv_str if srv_str else home_server
    else:
        # Fall back to trailing S\d+ on the alliance string for legacy raw files
        m = re.search(r'\bS(\d{2,4})\s*$', raw_ally)
        if m:
            server = m.group(1)
        else:
            server = home_server

    # Strip trailing server identifier (e.g. S119 or S113)
    clean_ally = re.sub(r'\s*S\d+\s*$', '', raw_ally).strip()
    norm_ally = ALLIANCE_NORMALIZATION_MAP.get(clean_ally, clean_ally)
    # Tag-only OCR variants need current-event counts before they can be repaired.
    if norm_ally != clean_ally and _same_name_tag_variant(clean_ally, norm_ally):
        norm_ally = clean_ally
    # Delimiter repairs need evidence from published alliances or the current majority.
    if (norm_ally != clean_ally and not re.match(r'^\[[^]]+\]', clean_ally)
            and norm_ally not in (known_alliances or set())):
        norm_ally = clean_ally

    tag, name = parse_tag_and_name(norm_ally)
    full_display = f"[{tag}] {name}".strip() if tag else (name or "No Alliance")

    return {
        "rank": rank,
        "commander": cmd,
        "alliance": full_display,
        "alliance_tag": tag,
        "alliance_name": name,
        "server": server,
        "points": pts
    }


def _normal_alliance(text):
    text = (text or "No Alliance").strip().translate(str.maketrans("［］", "[]"))
    tag, name = parse_tag_and_name(text)
    return f"[{tag}] {name}".strip() if tag else name


def build_known_alliances(events_dir=None, exclude_event=None):
    return {_normal_alliance(r.get("alliance")) for r in event_rankings(events_dir, exclude_event)}


def _tag_confusable(observed, canonical):
    if (len(observed) == len(canonical)
            and sum(a != b for a, b in zip(observed.casefold(), canonical.casefold())) <= 1
            and confusable_key(observed) == confusable_key(canonical)):
        return True
    # An extra I/J/U/1 is often a closing bracket fused to the tag.
    if abs(len(observed) - len(canonical)) == 1:
        long, short = (observed, canonical) if len(observed) > len(canonical) else (canonical, observed)
        return any(ch in "UIJ1ulij|" and confusable_key(long[:i] + long[i + 1:]) == confusable_key(short)
                   for i, ch in enumerate(long))
    return False


def _same_name_tag_variant(observed, canonical):
    observed_tag, observed_name = parse_tag_and_name(observed)
    canonical_tag, canonical_name = parse_tag_and_name(canonical)
    return bool(observed_tag and canonical_tag
                and observed_name.casefold() == canonical_name.casefold()
                and _tag_confusable(observed_tag, canonical_tag))


def canonicalize_alliances(records, known_alliances=None, overrides=None, review_candidates=None):
    """Repair small alliance tag variants with evidence; report the rest for review."""
    prior = {_normal_alliance(a) for a in (known_alliances or set())}
    overrides = overrides or {}
    review_candidates = review_candidates if review_candidates is not None else []
    observed = [overrides.get(row["alliance"], _normal_alliance(row["alliance"])) for row in records]
    current_counts = Counter(observed)
    current = set(current_counts)
    known = prior | current
    changes = []
    for row, normalized in zip(records, observed):
        before = row["alliance"]
        after = normalized
        reason = "manual override" if before in overrides else "known alliance"
        if before not in overrides:
            tag, name = parse_tag_and_name(normalized)
            candidates = set()
            if tag:
                candidates = {alliance for alliance in known
                              if alliance != normalized
                              and (alliance in prior
                                   or current_counts[alliance] > current_counts[normalized])
                              and _same_name_tag_variant(normalized, alliance)}
            else:
                # Safe malformed-bracket readings such as “UJQKI JQKKK” have
                # a distinctive delimiter pattern and stay automatic.
                for alliance in known:
                    known_tag, known_name = parse_tag_and_name(alliance)
                    if not known_tag or not normalized.casefold().endswith(known_name.casefold()):
                        continue
                    prefix = normalized[:-len(known_name)].strip()
                    if (len(prefix) >= 2 and prefix[0] in "[UIJ1" and prefix[-1] in "]UIJ1"
                            and _tag_confusable(prefix[1:-1], known_tag)):
                        candidates.add(alliance)
            if len(candidates) == 1 and not tag:
                after = candidates.pop()
                reason = "bracket repair"
            elif candidates:
                eligible = [candidate for candidate in candidates
                            if current_counts[normalized] <= 2
                            and (current_counts[candidate] > current_counts[normalized]
                                 or candidate in prior)]
                if len(eligible) == 1 and len(candidates) == 1:
                    after = eligible[0]
                else:
                    for candidate in sorted(candidates):
                        review_candidates.append({
                            "rank": row["rank"],
                            "server": str(row.get("server") or ""),
                            "observed": normalized,
                            "known": candidate,
                            "alliance": name or parse_tag_and_name(candidate)[1],
                        })
        row["alliance"] = after
        row["alliance_tag"], row["alliance_name"] = parse_tag_and_name(after)
        if after != before:
            changes.append({"rank": row["rank"], "field": "alliance", "before": before,
                            "after": after, "reason": reason})
    return changes


def clean_records(records, home_server="117", visiting_server="119", event_id=None, base_dir=None,
                  include_reviews=False):
    """Batch clean records; optionally return report-only name/alliance suggestions."""
    base_dir = base_dir or BASE_DIR
    events_dir = os.path.join(base_dir, "events")
    known_alliances = build_known_alliances(events_dir, event_id)
    alliance_overrides = load_overrides(os.path.join(base_dir, "data", "alliance_overrides.json"))
    cleaned = [clean_player_record(r, home_server, visiting_server, known_alliances) for r in records]
    reviews = {"name_review": [], "alliance_review": []}
    changes = canonicalize_alliances(cleaned, known_alliances, alliance_overrides,
                                     reviews["alliance_review"])
    index = build_known_name_index(events_dir, event_id)
    name_overrides = load_overrides(os.path.join(base_dir, "data", "name_overrides.json"))
    changes.extend(apply_known_names(cleaned, index, name_overrides, reviews["name_review"]))
    if include_reviews:
        return cleaned, changes, reviews
    return cleaned, changes

def validate_dataset(records):
    """Perform mathematical and logical integrity checks on the dataset."""
    errors = []
    ranks = [r["rank"] for r in records]
    max_rank = max(ranks) if ranks else 0

    # 1. Missing ranks
    missing = [r for r in range(1, max_rank + 1) if r not in set(ranks)]
    if missing:
        errors.append(f"Missing {len(missing)} ranks: {missing[:15]}...")

    # 2. Empty commanders
    empty_cmds = [r["rank"] for r in records if not r.get("commander")]
    if empty_cmds:
        errors.append(f"Empty commander names at ranks: {empty_cmds[:15]}")

    # 3. None points
    none_pts = [r["rank"] for r in records if r.get("points") is None]
    if none_pts:
        errors.append(f"Missing points at ranks: {none_pts[:15]}")

    # 4. Monotonic points check
    non_monotonic = []
    for i in range(1, len(records)):
        p_prev = records[i-1]["points"]
        p_curr = records[i]["points"]
        if p_prev is not None and p_curr is not None and p_curr > p_prev:
            non_monotonic.append((records[i-1]["rank"], p_prev, records[i]["rank"], p_curr))

    if non_monotonic:
        errors.append(f"Non-monotonic points at {len(non_monotonic)} positions: {non_monotonic[:5]}")

    return errors
