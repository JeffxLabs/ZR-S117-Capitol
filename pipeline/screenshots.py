#!/usr/bin/env python3
"""
Screenshot archiving for Capitol War captures.

Full-resolution frames are written to a local staging folder outside the repo
(LOCAL_CAPTURE_DIR/<event_id>/). After a capture they are compressed to 720px-wide
WebP (~50 KB each, still readable) into events/<event_id>/screenshots/ for the record.
The local staging copies are deleted after a few days by the cleanup agent
(tools/install_cleanup_agent.sh). The dashboard never loads these files.
"""
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

# Game server time (shown in-game as "State Time"); capture times are recorded in it.
SERVER_TZ = timezone(timedelta(hours=-2), "server (UTC-2)")

LOCAL_CAPTURE_DIR = os.path.expanduser("~/Library/Caches/s117-zroute-captures")
WIDTH = 720
QUALITY = 70


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


def archive_screenshots(sources, repo_event_dir, subdir="screenshots"):
    """Compress each source image into <repo_event_dir>/<subdir>/.

    sources: list of (path, name, kind) where name is the output basename (no extension)
    and kind is a short label such as "capture-frame" or "verification".
    Returns manifest entries: [{file, kind, taken_at}] ordered by capture time.
    """
    out_dir = os.path.join(repo_event_dir, subdir)
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    for src, name, kind in sources:
        st = os.stat(src)
        taken = datetime.fromtimestamp(st.st_mtime, SERVER_TZ)
        dest = _compress_one(src, os.path.join(out_dir, name))
        entries.append({
            "file": os.path.relpath(dest, repo_event_dir),
            "kind": kind,
            "taken_at": taken.isoformat(timespec="seconds"),
        })
    entries.sort(key=lambda e: (e["taken_at"], e["file"]))
    return entries
