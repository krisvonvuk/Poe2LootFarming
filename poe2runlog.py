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

How the watcher works (important if you had in-game interference):
- On Wayland the watcher is EVENT-DRIVEN: a single persistent
  'wl-paste --watch' process notifies us only when the clipboard
  changes. Nothing is polled, so the game keeps full keyboard focus.
  Earlier versions spawned 'wl-paste' every 0.4s; each spawn made the
  game window briefly lose keyboard focus, which caused item tooltips
  to close after 1-2 seconds and the Ctrl hotbar swap key to appear to
  toggle on/off while held. Do not go back to polling on Wayland.
- On X11 (xclip has no watch mode) we poll every POE2_POLL seconds
  (default 0.5). If you see interference under X11, raise it:
  POE2_POLL=2.0 python3 poe2runlog.py start

Notes:
- Only clipboard text that looks like a PoE item (starts with "Item Class:")
  is logged; everything else on your clipboard is ignored.
- The raw item text is always stored, so nothing is lost even when the
  quick parser misses a field; the agent re-parses raw text later.
- A run_start record is written when the watcher starts, so the agent
  gets an accurate session start time even if you copy nothing for a while.

This tool is a personal, non-commercial helper. It runs completely
separate from the game, reads only the clipboard you explicitly copy
to, sends no input to the game, and reads no game files or memory.
This product isn't affiliated with or endorsed by Grinding Gear
Games in any way.
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
POLL_SECONDS = float(os.environ.get("POE2_POLL", "0.5"))


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def ensure_runs_dir():
    os.makedirs(RUNS_DIR, exist_ok=True)


def clipboard_tool():
    if shutil.which("wl-paste"):
        return "wayland"
    if shutil.which("xclip"):
        return "x11"
    return None


def clipboard_get():
    """One-shot clipboard read. Works on both Wayland (plain read, no
    --watch) and X11."""
    if shutil.which("wl-paste"):
        try:
            proc = subprocess.run(["wl-paste", "--no-newline"],
                                  capture_output=True, text=True, timeout=2)
            if proc.returncode == 0:
                return proc.stdout
        except Exception:
            pass
    if shutil.which("xclip"):
        try:
            proc = subprocess.run(["xclip", "-o", "-selection", "clipboard"],
                                  capture_output=True, text=True, timeout=2)
            if proc.returncode == 0:
                return proc.stdout
        except Exception:
            pass
    return None


def wayland_watch_supported():
    """True if this compositor supports the wlr data-control protocol
    that 'wl-paste --watch' needs (GNOME, among others, does not)."""
    try:
        proc = subprocess.run(["wl-paste", "--watch", "true"],
                              capture_output=True, text=True, timeout=5)
        return proc.returncode == 0
    except Exception:
        return False


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


def read_run_state(path):
    """Rebuild waystone/tablet/loot/gear state from the run file.

    The Wayland watcher is a single 'wl-paste --watch' parent with a fresh
    helper process per clipboard event, so state is persisted in the run
    file itself and rebuilt here. This also makes the logger robust
    against crashes: nothing in memory is authoritative.
    """
    state = {"used_waystone": False, "loot_seen": False,
             "tablets_used": 0, "gear_iir": 0, "gear_count": 0}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "item":
                    continue
                role = rec.get("role")
                if role == "waystone_used":
                    state["used_waystone"] = True
                elif role == "tablet_used":
                    state["tablets_used"] += 1
                elif role == "loot":
                    state["loot_seen"] = True
                elif role == "gear" and rec.get("iir"):
                    state["gear_iir"] += rec["iir"]
                    state["gear_count"] += 1
    except FileNotFoundError:
        pass
    return state


def classify_copy(path, mode, raw):
    """Parse a clipboard copy, classify it, append it. Returns role or
    None if the text is not a PoE item."""
    if ITEM_SIG not in raw:
        return None
    state = read_run_state(path)
    item = parse_item(raw) or {}
    rec = {"t": now_iso(), "type": "item", "raw": raw}
    rec.update(item)
    if mode == "gear":
        rec["role"] = "gear"
        state["gear_iir"] += item.get("iir", 0)
        state["gear_count"] += 1
    else:
        hay = ((item.get("item_class") or "") + " " +
               (item.get("base_type") or "")).lower()
        if "waystone" in hay and not state["used_waystone"]:
            rec["role"] = "waystone_used"
        elif ("tablet" in hay and not state["loot_seen"]
              and state["tablets_used"] < 4):
            rec["role"] = "tablet_used"
        else:
            rec["role"] = "loot"
    append(path, rec)
    if mode == "gear":
        append(path, {"t": now_iso(), "type": "gear_total",
                      "iir": state["gear_iir"], "items": state["gear_count"]})
        print(f"  logged: {item.get('base_type') or '?'}  ->  gear "
              f"(running total +{state['gear_iir']}%)")
    else:
        print(f"  logged: {item.get('base_type') or '?'}  ->  {rec['role']}")
    return rec["role"]


def cmd_oncopy(path, mode):
    """Called (by wl-paste --watch) with the new clipboard text on stdin."""
    raw = sys.stdin.read()
    try:
        classify_copy(path, mode, raw)
    except Exception as exc:
        print(f"  (failed to log copy: {exc})", file=sys.stderr)


def watch_wayland(path, mode):
    """Event-driven watcher: one persistent wl-paste process, no polling.

    'wl-paste --watch CMD' runs CMD with the new clipboard content on
    stdin each time the clipboard changes. Because the parent stays
    alive for the whole session and no per-poll subprocess is spawned,
    the game never loses keyboard focus.

    Note: --no-newline CANNOT be combined with --watch (wl-clipboard
    errors out immediately); the watched copy includes the trailing
    newline, which parse_item tolerates.
    """
    cmd = ["wl-paste", "--watch",
           sys.executable, os.path.abspath(__file__), "_oncopy", path, mode]
    print("Watching clipboard (event-driven). Ctrl+C here to stop.")
    restarts = 0
    try:
        while True:
            proc = subprocess.Popen(cmd)
            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                raise
            if rc == 0:
                # clean exit (e.g. wl-paste --watch stopping because the
                # clipboard manager went away); restart to keep watching
                pass
            else:
                print(f"  (wl-paste exited with code {rc}; restarting)")
            restarts += 1
            if restarts > 10:
                sys.exit("wl-paste keeps exiting; run 'wl-paste --watch echo test' "
                         "in another terminal to debug.")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    if mode == "gear":
        state = read_run_state(path)
        print(f"Gear rarity total: +{state['gear_iir']}% Increased Rarity "
              f"of Items found (from {state['gear_count']} copied items)")
    print("Watcher stopped. Run file saved.")


def watch_wayland_poll(path, mode):
    """Wayland polling fallback for compositors WITHOUT wlr data-control
    (e.g. GNOME). Polls a plain 'wl-paste' read every POE2_POLL seconds.

    This is the same design as the original logger, which some users
    found caused in-game focus flicker (tooltips closing, Ctrl hotbar
    toggling). If you see that, either raise the interval:
      POE2_POLL=2.0 python3 poe2runlog.py start
    or run the game under a wlroots-compatible session where the
    event-driven watcher is used instead."""
    seen = None
    print(f"Watching clipboard (Wayland polling, every {POLL_SECONDS}s - "
          "compositor lacks watch support). Ctrl+C here to stop.")
    try:
        while True:
            raw = clipboard_get()
            if raw is not None and raw != seen:
                seen = raw
                classify_copy(path, mode, raw)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    if mode == "gear":
        state = read_run_state(path)
        print(f"Gear rarity total: +{state['gear_iir']}% Increased Rarity "
              f"of Items found (from {state['gear_count']} copied items)")
    print("Watcher stopped. Run file saved.")


def watch_x11(path, mode):
    """X11 fallback: xclip has no watch mode, so we must poll."""
    seen = None
    print(f"Watching clipboard (polling every {POLL_SECONDS}s, X11 mode). "
          "Ctrl+C here to stop.")
    try:
        while True:
            raw = clipboard_get()
            if raw is not None and raw != seen:
                seen = raw
                classify_copy(path, mode, raw)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    if mode == "gear":
        state = read_run_state(path)
        print(f"Gear rarity total: +{state['gear_iir']}% Increased Rarity "
              f"of Items found (from {state['gear_count']} copied items)")
    print("Watcher stopped. Run file saved.")


def watch(path, mode):
    tool = clipboard_tool()
    if tool is None:
        sys.exit("No clipboard tool found. Install one:\n"
                 "  sudo apt install wl-clipboard   (Wayland)\n"
                 "  sudo apt install xclip          (X11)")
    if tool == "wayland":
        if wayland_watch_supported():
            watch_wayland(path, mode)
        else:
            watch_wayland_poll(path, mode)
    else:
        watch_x11(path, mode)


def latest_run():
    ensure_runs_dir()
    files = sorted(f for f in os.listdir(RUNS_DIR)
                   if f.startswith("run_") and f.endswith(".jsonl"))
    if not files:
        sys.exit("No run files yet. Start with: python3 poe2runlog.py start")
    return os.path.join(RUNS_DIR, files[-1])


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
    elif cmd == "_oncopy" and len(sys.argv) >= 4:
        cmd_oncopy(sys.argv[2], sys.argv[3])
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
