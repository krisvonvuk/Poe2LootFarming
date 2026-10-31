#!/usr/bin/env python3
"""poe2runlog - passive per-run logger for Path of Exile 2 map sessions.

Creates one file per map run (runs/run_<timestamp>.jsonl) containing the
waystone you used, every tablet you used, every item you Ctrl+C'd, your gear
rarity total, and free-text notes. The recommendation agent later parses
these files into its prediction dataset.

Workflow (easiest possible):
  1. python3 poe2runlog.py start
       Creates a fresh run file and watches your clipboard.
  2. Before entering the map, hover and Ctrl+C your Waystone first,
     then EVERY Tablet you are slotting (up to 4). Waystone/tablet
     copies made BEFORE the first drop copy are tagged "used"; any
     copy made after the first loot item (including waystone/tablet
     drops found mid-map) is tagged "loot".
  3. While or after mapping, Ctrl+C every drop you care about - the same
     copies you already make for price checking are captured automatically.
  4. Press Ctrl+C in this terminal to stop the watcher.
  5. python3 poe2runlog.py note "savannah, desert biome, 8 min, 0 deaths"
     One line of free text: biome, map name, clear time, deaths, anything
     else. The agent parses this into structured fields.
  6. Optional, once per session or after a gear change:
     python3 poe2runlog.py gear
     Copy each equipped item once; the script sums your total Increased
     Rarity of Items found and stores it in the run file.
  7. python3 poe2runlog.py summary
     Prints what the latest run file contains.

No pip dependencies. Requires wl-clipboard (Wayland) or xclip (X11):
    sudo apt install wl-clipboard

Notes:
- Only clipboard text that looks like a PoE item (starts with "Item Class:")
  is logged; everything else on your clipboard is ignored.
- The raw item text is always stored, so nothing is lost even when the
  quick parser misses a field; the agent re-parses raw text later.
- A run_start record is written when the watcher starts, so the agent
  gets an accurate session start time even if you copy nothing for a while.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(BASE_DIR, "runs")

ITEM_SIG = "Item Class:"
IIR_RE = re.compile(r"(\d+)\s*%\s*increased Rarity of Items found")
WS_TIER_RE = re.compile(r"Waystone \(Tier (\d+)\)")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def ensure_runs_dir():
    os.makedirs(RUNS_DIR, exist_ok=True)


def clipboard_get():
    """Return current clipboard text, or None. Wayland first, X11 fallback."""
    for cmd in (["wl-paste", "--no-newline"],
                ["xclip", "-o", "-selection", "clipboard"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=2)
            if proc.returncode == 0:
                return proc.stdout
        except Exception:
            continue
    return None


def parse_item(raw):
    """Light parse of copied PoE item text. Full raw text is stored anyway."""
    lines = [ln.rstrip() for ln in raw.splitlines()]
    idx = next((i for i, ln in enumerate(lines) if ln.startswith(ITEM_SIG)),
               None)
    if idx is None:
        return None
    item = {"item_class": lines[idx].split(":", 1)[1].strip()}

    ridx = next((i for i, ln in enumerate(lines) if ln.startswith("Rarity:")),
                None)
    if ridx is not None:
        item["rarity"] = lines[ridx].split(":", 1)[1].strip()
        block = []
        for ln in lines[ridx + 1:]:
            s = ln.strip()
            if s == "" or s.startswith("---"):
                if block:
                    break
                continue
            block.append(s)
        if block:
            item["base_type"] = block[-1]
            item["name"] = " ".join(block[:-1]) if len(block) > 1 else block[-1]

    if item.get("item_class", "").lower() == "waystones":
        m = WS_TIER_RE.search(raw)
        if m:
            item["waystone_tier"] = int(m.group(1))
    m = IIR_RE.search(raw)
    if m:
        item["iir"] = int(m.group(1))
    return item


def append(path, record):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def latest_run():
    ensure_runs_dir()
    files = sorted(f for f in os.listdir(RUNS_DIR)
                   if f.startswith("run_") and f.endswith(".jsonl"))
    if not files:
        sys.exit("No run files yet. Start with: python3 poe2runlog.py start")
    return os.path.join(RUNS_DIR, files[-1])


def watch(path, mode):
    seen = None
    used_waystone = False
    loot_seen = False
    tablets_used = 0
    gear_total = 0
    gear_count = 0
    if shutil.which("wl-paste") is None and shutil.which("xclip") is None:
        sys.exit("No clipboard tool found. Install one:\n"
                 "  sudo apt install wl-clipboard   (Wayland)\n"
                 "  sudo apt install xclip          (X11)")
    print("Watching clipboard. Ctrl+C here to stop.")
    try:
        while True:
            raw = clipboard_get()
            if raw is not None and raw != seen:
                seen = raw
                if ITEM_SIG in raw:
                    item = parse_item(raw) or {}
                    rec = {"t": now_iso(), "type": "item", "raw": raw}
                    rec.update(item)
                    if mode == "gear":
                        rec["role"] = "gear"
                        if item.get("iir"):
                            gear_total += item["iir"]
                            gear_count += 1
                    else:
                        hay = ((item.get("item_class") or "") + " " +
                               (item.get("base_type") or "")).lower()
                        if "waystone" in hay and not used_waystone:
                            rec["role"] = "waystone_used"
                            used_waystone = True
                        elif ("tablet" in hay and not loot_seen
                              and tablets_used < 4):
                            rec["role"] = "tablet_used"
                            tablets_used += 1
                        else:
                            rec["role"] = "loot"
                            loot_seen = True
                    append(path, rec)
                    label = item.get("base_type") or "?"
                    print(f"  logged: {label}  ->  {rec['role']}")
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass
    if mode == "gear":
        append(path, {"t": now_iso(), "type": "gear_total",
                      "iir": gear_total, "items": gear_count})
        print(f"Gear rarity total: +{gear_total}% Increased Rarity of Items "
              f"found (from {gear_count} copied items)")
    print("Watcher stopped. Run file saved.")


def cmd_start():
    ensure_runs_dir()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RUNS_DIR, f"run_{ts}.jsonl")
    append(path, {"t": now_iso(), "type": "run_start"})
    print(f"New run file: {path}")
    print("Copy your WAYSTONE first, then your TABLETS, then play.")
    watch(path, "run")


def cmd_note(text):
    path = latest_run()
    append(path, {"t": now_iso(), "type": "note", "text": text})
    print(f"Note saved to {os.path.basename(path)}")


def cmd_gear():
    path = latest_run()
    print("Copy each equipped item once. Ctrl+C here when done.")
    watch(path, "gear")


def cmd_summary():
    path = latest_run()
    print(f"Summary of {os.path.basename(path)}")
    loot = 0
    tablets = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec["type"] == "item" and rec.get("role") == "waystone_used":
                print(f"  Waystone used : {rec.get('name', '?')} "
                      f"(Tier {rec.get('waystone_tier', '?')})")
            elif rec["type"] == "item" and rec.get("role") == "tablet_used":
                tablets.append(rec.get("base_type", "?"))
            elif rec["type"] == "item" and rec.get("role") == "loot":
                loot += 1
            elif rec["type"] == "gear_total":
                print(f"  Gear rarity   : +{rec['iir']}% "
                      f"({rec['items']} items)")
            elif rec["type"] == "note":
                print(f"  Note          : {rec['text']}")
            elif rec["type"] == "run_start":
                print(f"  Started       : {rec.get('t', '?')}")
    if tablets:
        print(f"  Tablets used  : {', '.join(tablets)}")
    print(f"  Loot captured : {loot} items")
    print("Full modifier text of everything is in the file's 'raw' fields.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1].lower()
    if cmd == "start":
        cmd_start()
    elif cmd == "note" and len(sys.argv) > 2:
        cmd_note(" ".join(sys.argv[2:]))
    elif cmd == "gear":
        cmd_gear()
    elif cmd == "summary":
        cmd_summary()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
