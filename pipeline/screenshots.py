#!/usr/bin/env python3
"""
Screenshot archiving for Capitol War captures.

Full-resolution frames are written to a local staging folder outside the repo
(LOCAL_CAPTURE_DIR/<event_id>/). Selected frames are compressed to 720px-wide WebP
(~50 KB each, still readable) into events/<event_id>/screenshots/ for the record.
Repair-seek frames stay local, and only two terminal no-new-rank capture frames are
archived. The local staging copies are deleted after a few days by the cleanup agent
(tools/install_cleanup_agent.sh). The dashboard never loads these files.
"""
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

# Game server time (shown in-game as "State Time"); capture times are recorded in it.
SERVER_TZ = timezone(timedelta(hours=-2), "server (UTC-2)")

LOCAL_CAPTURE_DIR = os.path.expanduser("~/Library/Caches/s117-zroute-captures")
WIDTH = 720
QUALITY = 70
MAIN_CAPTURE_FRAME = re.compile(r"frame_\d{4}\.png$")


def staging_dir(event_id):
    path = os.path.join(LOCAL_CAPTURE_DIR, event_id)
    os.makedirs(path, exist_ok=True)
    return path


def _compress_one(src, dest_base):
    """Compress src to dest_base.webp (cwebp) or dest_base.jpg (sips fallback). Returns the path written."""
    if shutil.which("cwebp"):
        dest = dest_base + ".webp"
        subprocess.run(["cwebp", "-quiet", "-q", str(QUALITY), "-resize", str(WIDTH), "0", src, "-o", dest], check=True)
    else:
        dest = dest_base + ".jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(QUALITY),
                        "--resampleWidth", str(WIDTH), src, "--out", dest],
                       check=True, stdout=subprocess.DEVNULL)
    return dest


def select_screenshots_to_archive(filenames, frame_info):
    """Select source basenames to archive, retaining only two terminal stall frames.

    ``frame_info`` is the ordered list of raw observation frame records. Rank ids
    are accumulated in that order, so a main capture frame with no previously
    unseen rows can be recognized as part of the terminal bottom stall.
    Repair-seek frames are navigation-only and always stay in the local cache.
    """
    candidates = list(filenames)
    seen_ranks = set()
    new_ranks_by_frame = {}
    for frame in frame_info or []:
        frame_name = os.path.basename(frame.get("frame", ""))
        ranks = {row.get("rank") for row in frame.get("rows", [])
                 if isinstance(row.get("rank"), int)}
        if MAIN_CAPTURE_FRAME.fullmatch(frame_name):
            new_ranks_by_frame[frame_name] = ranks - seen_ranks
        seen_ranks.update(ranks)

    main_frames = sorted(
        os.path.basename(name) for name in candidates
        if MAIN_CAPTURE_FRAME.fullmatch(os.path.basename(name))
    )
    terminal_stalls = []
    for frame_name in reversed(main_frames):
        if frame_name not in new_ranks_by_frame or new_ranks_by_frame[frame_name]:
            break
        terminal_stalls.append(frame_name)
    discard_stalls = set(terminal_stalls[2:])

    return [name for name in candidates
            if not os.path.basename(name).startswith("repair_seek_")
            and os.path.basename(name) not in discard_stalls]


def archive_screenshots(sources, repo_event_dir, subdir="screenshots"):
    """Compress each source image into <repo_event_dir>/<subdir>/.

    sources: list of (path, name, kind[, taken_at]) where name is the output basename (no extension)
    and kind is a short label such as "capture-frame" or "verification".
    Returns manifest entries: [{file, kind, taken_at}] ordered by capture time.
    """
    out_dir = os.path.join(repo_event_dir, subdir)
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    for source in sources:
        src, name, kind = source[:3]
        taken = (source[3].astimezone(SERVER_TZ) if len(source) > 3
                 else datetime.fromtimestamp(os.stat(src).st_mtime, SERVER_TZ))
        dest = _compress_one(src, os.path.join(out_dir, name))
        entries.append({
            "file": os.path.relpath(dest, repo_event_dir),
            "kind": kind,
            "taken_at": taken.isoformat(timespec="seconds"),
        })
    entries.sort(key=lambda e: (e["taken_at"], e["file"]))
    return entries
