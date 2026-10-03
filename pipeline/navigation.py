#!/usr/bin/env python3
"""
OCR-driven navigation for the capture:

  go_to_rankings():  back out of whatever is open -> "Expedition Frenzy" (right side of the
                     map/city HUD) -> "Capitol Conquest" tab (top right) -> "Rankings" button.
  return_to_city():  back out of the rankings -> "RETURN TO CITY" on the world map.

Every step re-reads the screen with OCR and taps the matched label's centre, so it does not
depend on fixed coordinates. A screenshot of each step is kept in the local staging folder.
"""
import os
import re
import struct
import subprocess
import time

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_BIN = os.path.join(PIPELINE_DIR, "vision_ocr")
BACK_KEY = "4"


class NavigationError(RuntimeError):
    pass


class Screen:
    def __init__(self, items, width, height, path):
        self.items = items
        self.width = width
        self.height = height
        self.path = path
        self.texts = [i["text"].strip() for i in items]
        self.joined = " | ".join(self.texts)

    def find(self, pattern, region=None):
        """First OCR box whose text matches `pattern` (case-insensitive regex), optionally within
        region=(x0, x1, top0, top1) in normalized coordinates measured from the top-left."""
        rx = re.compile(pattern, re.I)
        for it in self.items:
            if not rx.search(it["text"]):
                continue
            cx = it["x"] + it["width"] / 2
            top = 1 - (it["y"] + it["height"] / 2)
            if region and not (region[0] <= cx <= region[1] and region[2] <= top <= region[3]):
                continue
            return it
        return None

    def has(self, *patterns):
        return all(self.find(p) for p in patterns)

    def center_px(self, item):
        cx = (item["x"] + item["width"] / 2) * self.width
        cy = (1 - (item["y"] + item["height"] / 2)) * self.height
        return int(cx), int(cy)


class Navigator:
    def __init__(self, adb, device, shots_dir, log=print):
        self.adb = adb
        self.device = device
        self.shots_dir = shots_dir
        self.log = log
        self.step = 0
        os.makedirs(shots_dir, exist_ok=True)

    def _adb(self, *args, **kw):
        return subprocess.run([self.adb, "-s", self.device, *args], **kw)

    def read(self, label):
        self.step += 1
        path = os.path.join(self.shots_dir, f"nav_{self.step:02d}_{label}.png")
        with open(path, "wb") as f:
            self._adb("exec-out", "screencap", "-p", stdout=f, check=True)
        with open(path, "rb") as f:
            head = f.read(24)
        width, height = struct.unpack(">II", head[16:24])
        import json
        items = json.loads(subprocess.check_output([OCR_BIN, path]))
        return Screen(items, width, height, path)

    def tap(self, screen, item, what, wait=2.0):
        x, y = screen.center_px(item)
        self.log(f"  tap {what!r} at ({x},{y})")
        self._adb("shell", "input", "tap", str(x), str(y), check=True)
        time.sleep(wait)

    def back(self, wait=1.5):
        self.log("  back")
        self._adb("shell", "input", "keyevent", BACK_KEY, check=True)
        time.sleep(wait)

    # ----- screen classification -----
    @staticmethod
    def is_rankings(s):
        return s.has(r"^RANKINGS$") and s.find(r"^Commander$") and s.find(r"^Points$")

    @staticmethod
    def conquest_tab(s):
        return (s.find(r"Capit[ao]l\s*Conquest", region=(0.3, 1.0, 0.0, 0.3))
                or s.find(r"^Capit[ao]l$", region=(0.3, 1.0, 0.0, 0.3))
                or s.find(r"Conquest", region=(0.3, 1.0, 0.0, 0.3)))

    @staticmethod
    def conquest_rankings_button(s):
        # A "Rankings" button below the page header (the header itself reads RANKINGS only on the list page)
        return s.find(r"^Rankings?$", region=(0.0, 1.0, 0.12, 1.0))

    @staticmethod
    def expedition_icon(s):
        return s.find(r"^Expedition(\s*Frenzy)?$", region=(0.6, 1.0, 0.0, 0.8))

    @staticmethod
    def exit_dialog_cancel(s):
        if s.find(r"(exit|quit)\s*(the\s*)?game|are you sure", ) or (s.find(r"^(Exit|Quit)$") and s.find(r"^Cancel$")):
            return s.find(r"^Cancel$")
        return None

    @staticmethod
    def return_to_city_btn(s):
        return s.find(r"RETURN\s*TO", region=(0.7, 1.0, 0.85, 1.0)) or s.find(r"^CITY$", region=(0.7, 1.0, 0.85, 1.0))

    @staticmethod
    def on_world_or_city(s):
        return bool(s.find(r"^Alliance$") and (s.find(r"^Mail$") or s.find(r"^Bag$")))

    # ----- flows -----
    def _read_until(self, label, check, tries=4, wait=1.5):
        """Re-read the screen until check(screen) returns something (pages can take a moment to load)."""
        s = None
        for _ in range(tries):
            s = self.read(label)
            found = check(s)
            if found:
                return s, found
            time.sleep(wait)
        return s, None

    def go_to_rankings(self):
        """Fixed path only: main city screen -> Expedition Frenzy -> Capitol Conquest tab -> Rankings.
        (The Rankings button on the cabinet/President screen opens the server-wide rankings,
        so no shortcut from other pages is taken.) Any unexpected screen raises NavigationError."""
        self.log("Navigating: main screen > Expedition Frenzy > Capitol Conquest > Rankings ...")
        self.return_to_city()

        s, icon = self._read_until("main", self.expedition_icon)
        if not icon:
            raise NavigationError(f"'Expedition Frenzy' not found on the main screen ({s.path})")
        self.tap(s, icon, "Expedition Frenzy", wait=3.0)

        s, tab = self._read_until("expedition_frenzy", self.conquest_tab)
        if not tab:
            raise NavigationError(f"'Capitol Conquest' tab not found in Expedition Frenzy ({s.path})")
        self.tap(s, tab, "Capitol Conquest tab", wait=3.0)

        s, btn = self._read_until("capitol_conquest", self.conquest_rankings_button)
        if not btn:
            raise NavigationError(f"'Rankings' button not found on the Capitol Conquest tab ({s.path})")
        self.tap(s, btn, "Rankings", wait=3.0)

        s, ok = self._read_until("rankings", self.is_rankings)
        if not ok:
            tab_btn = s.find(r"^Ranking$", region=(0.0, 0.5, 0.0, 0.15))
            if tab_btn and s.find(r"^RANKINGS$"):
                self.tap(s, tab_btn, "Ranking tab")
                s, ok = self._read_until("rankings", self.is_rankings)
        if not ok:
            raise NavigationError(f"Rankings page did not show the Rank/Commander/Points list ({s.path})")
        self.log("  on the Capitol Conquest Rankings list")
        return True

    def return_to_city(self, max_steps=12):
        self.log("Returning to city view ...")
        for _ in range(max_steps):
            s = self.read("to_city")
            cancel = self.exit_dialog_cancel(s)
            if cancel:
                self.tap(s, cancel, "Cancel (exit dialog)")
                continue
            btn = self.return_to_city_btn(s)
            if btn:
                self.tap(s, btn, "RETURN TO CITY", wait=3.5)
                continue
            if self.on_world_or_city(s):
                self.log("  in city view")
                return True
            self.back()
        raise NavigationError(f"Could not return to the city view in {max_steps} steps "
                              f"(last screen: {s.path})")
