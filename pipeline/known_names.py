#!/usr/bin/env python3
"""Conservative commander matching against previously published events."""
import json
import os
import unicodedata
from collections import Counter, defaultdict
from glob import glob

from merge import name_key

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFUSABLES = str.maketrans({
    "0": "o", "1": "l", "i": "l", "|": "l",
    "а": "a", "в": "b", "с": "c", "е": "e", "н": "h",
    "к": "k", "м": "m", "о": "o", "р": "p", "т": "t",
    "у": "y", "х": "x", "і": "l", "ј": "j", "ѕ": "s",
})


def confusable_key(text):
    text = unicodedata.normalize("NFKC", text or "").casefold().translate(_CONFUSABLES)
    return "".join(ch for ch in text if ch.isalnum())


def edit_distance(a, b, limit=None):
    """Levenshtein distance, optionally stopping once the limit cannot be met."""
    if limit is not None and abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (char_a != char_b)))
        if limit is not None and min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def event_rankings(events_dir=None, exclude_event=None):
    events_dir = events_dir or os.path.join(BASE_DIR, "events")
    for path in sorted(glob(os.path.join(events_dir, "*", "rankings.json"))):
        if os.path.basename(os.path.dirname(path)) == exclude_event:
            continue
        with open(path, encoding="utf-8") as f:
            yield from json.load(f)


def load_overrides(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class KnownNameIndex(dict):
    def __init__(self):
        super().__init__()
        self.alliances = defaultdict(set)


def build_known_name_index(events_dir=None, exclude_event=None):
    """Map (server, name_key) to the most common prior display spelling."""
    index = KnownNameIndex()
    spellings = defaultdict(Counter)
    for row in event_rankings(events_dir, exclude_event):
        commander = row.get("commander") or ""
        key = (str(row.get("server") or ""), name_key(commander))
        if not key[1] or commander == "(name unreadable by OCR)":
            continue
        spellings[key][commander] += 1
        index.alliances[key].add(row.get("alliance") or "No Alliance")
    for key, forms in spellings.items():
        index[key] = forms.most_common(1)[0][0]
    return index


def apply_known_names(records, index, overrides=None, review_candidates=None):
    """Apply manual name overrides and report fuzzy near-matches for review."""
    overrides = overrides or {}
    review_candidates = review_candidates if review_candidates is not None else []
    changes = []
    by_server = defaultdict(list)
    current_names = {
        (str(row.get("server") or ""), name_key(row.get("commander") or ""))
        for row in records
    }
    for key, canonical in index.items():
        by_server[key[0]].append((key, canonical, confusable_key(canonical)))
    for row in records:
        before = row.get("commander") or ""
        server = str(row.get("server") or "")
        override = overrides.get(f"{server}|{before}")
        after, reason = before, None
        if override is not None:
            after, reason = override, "manual override"
        elif before and (server, name_key(before)) not in index:
            observed = confusable_key(before)
            candidates = {
                canonical for key, canonical, normalized in by_server[server]
                if row.get("alliance") in index.alliances[key]
                and edit_distance(observed, normalized, limit=1) <= 1
            }
            candidates = {candidate for candidate in candidates
                          if (server, name_key(candidate)) not in current_names}
            for candidate in sorted(candidates):
                review_candidates.append({
                    "rank": row["rank"],
                    "server": server,
                    "observed": before,
                    "known": candidate,
                    "alliance": row.get("alliance") or "No Alliance",
                })
        if after != before:
            row["commander"] = after
            changes.append({"rank": row["rank"], "field": "commander", "before": before,
                            "after": after, "reason": reason})
    return changes
