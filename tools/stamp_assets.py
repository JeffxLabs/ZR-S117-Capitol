#!/usr/bin/env python3
"""Cache-busting: stamp content hashes into the asset URLs index.html loads.

- events/i18n.js and events/manifest.js get ?v=<hash of the file> in index.html
- every manifest entry gets "data_version" = hash of its event_data.js, which index.html
  appends to that event's script URL

Run after changing translations or event data (tools/build_i18n.py and the pipeline call it):
  python3 tools/stamp_assets.py
"""
import hashlib
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hash(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()[:10]


def stamp():
    events = os.path.join(BASE, "events")
    manifest_path = os.path.join(events, "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    for entry in manifest:
        data_js = os.path.join(events, entry["id"], "event_data.js")
        if os.path.exists(data_js):
            entry["data_version"] = _hash(data_js)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    with open(os.path.join(events, "manifest.js"), "w", encoding="utf-8") as f:
        f.write(f"window.EVENTS_MANIFEST = {json.dumps(manifest, indent=2, ensure_ascii=False)};\n")

    index_path = os.path.join(BASE, "index.html")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    for name in ("i18n.js", "manifest.js"):
        html = re.sub(r'(events/%s)\?v=[^"]*' % re.escape(name), r'\1?v=' + _hash(os.path.join(events, name)), html)
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    return manifest


if __name__ == "__main__":
    stamp()
    print("Stamped asset versions into index.html and events/manifest.*")
