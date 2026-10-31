#!/usr/bin/env python3
"""poe2agent - parse, price and enrich poe2runlog run files.

Pipeline: raw run JSONL (from poe2runlog.py)
   -> structured strategy + loot record
   -> loot priced in Chaos/Divine via poe.ninja
   -> free-text notes parsed by your local Ollama model
   -> one enriched JSON per run (the prediction dataset row)

Commands:
  sample                     write a sample run file to runs/samples/
  leagues                    list active PoE2 leagues known to poe.ninja
  parse   <runfile>          parse run JSONL into structured JSON
  notes   <runfile>          parse free-text notes via local Ollama
  price   <runfile>          price loot in Chaos/Divine via poe.ninja
  report  <runfile>          parse + notes + price, save enriched JSON
  raw     <league> <type>    dump a raw poe.ninja response for debugging

Usage:
  python3 poe2agent.py report runs/run_20260101_120000.jsonl
  python3 poe2agent.py report runs/run_x.jsonl --league "Forbidden Rites"

Environment:
  POE2_LEAGUE   league name for pricing (fallback: --league, then first active)
  POE2_PATCH    patch tag written to enriched runs (default: 0.5.5)
  POE2_MODEL    Ollama model tag (default: qwen3:8b)
  OLLAMA_URL    Ollama base URL (default: http://localhost:11434)

Only the Python standard library is used. Put this file in the project root
next to poe2runlog.py; it writes to ./data/.

Pricing uses poe.ninja's documented public PoE2 economy endpoints:
  /poe2/api/economy/leagues
  /poe2/api/economy/exchange/current/overview?league=..&type=..   (currencies)
  /poe2/api/economy/stash/current/item/overview?league=..&type=.. (uniques, tablets)
Exchange lines carry currency ids; names are joined from core.items.
Units (verified against live responses): exchange primaryValue is in
Divine Orbs (divine=1); item overview primaryValue is in Exalted Orbs
despite the response core claiming divine - item values are converted
with the exchange Exalted rate. Raw responses go to data/raw/.

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
import sqlite3
import sys
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PARSED_DIR = os.path.join(DATA_DIR, "parsed")
RAW_DIR = os.path.join(DATA_DIR, "raw")
PRICES_DB = os.path.join(DATA_DIR, "prices.db")
RUNS_DIR = os.path.join(BASE_DIR, "runs")
SAMPLE_DIR = os.path.join(RUNS_DIR, "samples")

POE_NINJA_BASE = "https://poe.ninja/poe2/api"
OLLAMA_URL = os.environ.get(
    "OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/generate"
USER_AGENT = "poe2-lootfarm-agent/0.1 (personal local tool)"
DEFAULT_MODEL = os.environ.get("POE2_MODEL", "qwen3:8b")
DEFAULT_PATCH = os.environ.get("POE2_PATCH", "0.5.5")
CACHE_SECONDS = 3600

# poe.ninja PoE2 exchange and stash item-overview categories.
EXCHANGE_TYPES = ("Currency", "Fragments", "Abyss", "UncutGems",
                  "LineageSupportGems", "Essences", "SoulCores", "Runes",
                  "Idols", "Ritual", "Expedition", "Delirium", "Breach",
                  "Verisium")
ITEM_TYPES = ("UniqueWeapons", "UniqueArmours", "UniqueAccessories",
               "UniqueFlasks", "UniqueCharms", "UniqueJewels",
               "UniqueSanctumRelics", "UniqueTablets", "PrecursorTablets")
MOD_RE = re.compile(r"(\d+%|increased|reduced|\bmore\b|\bless\b|Monsters|Players)")
STACK_RE = re.compile(r"Stack Size: (\d+)(?:/(\d+))?")
# The five bonuses a waystone mod can pay (0.5.x); no quantity line exists.
BONUS_RES = [
    ("rarity", re.compile(r"(\d+)%\s+increased Rarity of Items found", re.I)),
    ("pack_size", re.compile(r"(\d+)%\s+more Pack Size", re.I)),
    ("monster_rarity", re.compile(r"(\d+)%\s+increased (?:Monster Rarity|Rare Monsters|Rarity of Monsters)", re.I)),
    ("effectiveness", re.compile(r"(\d+)%\s+increased Effectiveness", re.I)),
    ("waystone_drop", re.compile(r"(\d+)%\s+increased chance of Waystones", re.I)),
]


def ensure_dirs():
    for d in (DATA_DIR, PARSED_DIR, RAW_DIR, SAMPLE_DIR):
        os.makedirs(d, exist_ok=True)


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ollama_generate(prompt, model, expect_json=True):
    """POST to local Ollama. Tries with think disabled first (Qwen3.x
    thinking output breaks JSON mode), then without the flag."""
    attempts = [{"model": model, "prompt": prompt, "stream": False,
                 "think": False},
                {"model": model, "prompt": prompt, "stream": False}]
    if expect_json:
        for payload in attempts:
            payload["format"] = "json"
    last_exc = None
    for payload in attempts:
        try:
            req = urllib.request.Request(
                OLLAMA_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            return out.get("response", "")
        except Exception as exc:
            last_exc = exc
            continue
    raise last_exc


def extract_json(text):
    """Parse JSON, tolerating surrounding prose or thinking output."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"{.*}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ---------------------------------------------------------------- parsing

def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def extract_mods(raw):
    """Pull modifier lines out of a copied waystone/tablet item text."""
    mods = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith("Item Class:") or s.startswith("Rarity:"):
            continue
        if s.startswith("-") or s.startswith("You") or s.startswith("Travel"):
            continue
        if MOD_RE.search(s):
            mods.append(s)
    return mods


def parse_waystone_bonuses(mods):
    """Numeric values for the five canonical waystone bonuses."""
    bonuses = {}
    for key, rx in BONUS_RES:
        for m in mods:
            hit = rx.search(m)
            if hit:
                bonuses[key] = max(bonuses.get(key, 0), int(hit.group(1)))
    return bonuses


def parse_run(path):
    run = {"source": os.path.basename(path),
           "waystone_used": None, "tablets_used": [], "tablet_used": None,
           "gear_iir": None, "loot": [], "notes": [], "started_at": None}
    gear_totals = []
    first_t = None
    for rec in read_jsonl(path):
        if first_t is None and rec.get("t"):
            first_t = rec["t"]
        rtype = rec.get("type")
        if rtype == "note":
            run["notes"].append(rec.get("text", ""))
        elif rtype == "gear_total":
            gear_totals.append(rec)
        elif rtype != "item":
            continue
        item = {
            "captured_at": rec.get("t"),
            "item_class": rec.get("item_class"),
            "rarity": rec.get("rarity"),
            "name": rec.get("name"),
            "base_type": rec.get("base_type"),
        }
        m = STACK_RE.search(rec.get("raw", ""))
        if m:
            item["stack"] = int(m.group(1))
        if rec.get("waystone_tier"):
            item["tier"] = rec["waystone_tier"]
        role = rec.get("role")
        if role == "waystone_used":
            item["mods"] = extract_mods(rec.get("raw", ""))
            item["bonuses"] = parse_waystone_bonuses(item["mods"])
            run["waystone_used"] = item
        elif role == "tablet_used":
            item["mods"] = extract_mods(rec.get("raw", ""))
            run["tablets_used"].append(item)
        elif role == "loot":
            item["count"] = item.get("stack", 1)
            run["loot"].append(item)
    if gear_totals:
        run["gear_iir"] = gear_totals[-1].get("iir")
    run["started_at"] = first_t
    # backward-compat single-tablet field (old strategy format)
    if run["tablets_used"]:
        run["tablet_used"] = run["tablets_used"][0]
    return run


def parse_notes_regex(notes):
    """Deterministic fallback when Ollama is unavailable. Handles the
    common 'savannah, desert biome, 8 min, 0 deaths' free-text style."""
    text = " | ".join(notes).lower()
    out = {"biome": "unknown", "map_name": None, "duration_min": None,
           "deaths": None, "extra": None}
    for biome in ("grass", "forest", "swamp", "desert", "mountain",
                 "water", "city", "savannah", "jungle", "volcano"):
        if biome in text:
            out["biome"] = biome.capitalize()
            break
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|minutes|mins)\b", text)
    if m:
        out["duration_min"] = float(m.group(1))
    m = re.search(r"(\d+)\s*deaths?\b", text)
    if m:
        out["deaths"] = int(m.group(1))
    m = re.search(r"\b(?:on|map|area|running)\s+([a-z\d' ]{3,30}?)"
                  r"(?=,|\||$)", text)
    if m:
        out["map_name"] = m.group(1).strip()
    if out["duration_min"] is None and out["biome"] == "unknown" \
            and out["deaths"] is None:
        return None
    return out


def parse_notes(run, model):
    """Ollama first; deterministic regex fallback so biome/duration are
    never lost just because the LLM is down."""
    if not run["notes"]:
        return None
    parsed = parse_notes_regex(run["notes"])
    try:
        raw = ollama_generate(
            "Extract structured data from these notes a Path of Exile 2 "
            "player wrote after a map run. Return JSON with keys: biome "
            "(one of Grass, Forest, Swamp, Desert, Mountain, Water, City, "
            "unknown), map_name (string), duration_min (number or null), "
            "deaths (number or null), extra (short string). Notes: "
            + " | ".join(run["notes"]), model)
        parsed = extract_json(raw)
    except Exception as exc:
        print(f"  (Ollama notes parsing skipped: {exc}; "
              "using deterministic fallback)")
    return parsed


def parse_notes_ollama(run, model):
    if not run["notes"]:
        return None
    prompt = (
        "Extract structured data from these notes a Path of Exile 2 player "
        "wrote after a map run. Return JSON with keys: biome (one of Grass, "
        "Forest, Swamp, Desert, Mountain, Water, City, unknown), map_name "
        "(string), duration_min (number or null), deaths (number or null), "
        "extra (any remaining useful info as short string). "
        "Notes: " + " | ".join(run["notes"])
    )
    try:
        raw = ollama_generate(prompt, model)
        return extract_json(raw)
    except Exception as exc:
        print(f"  (notes parsing skipped: {exc})")
        return None


# ---------------------------------------------------------------- pricing

def fetch_exchange(league, etype):
    """Exchange overview: lines carry ids, names live in core.items."""
    url = (f"{POE_NINJA_BASE}/economy/exchange/current/overview"
           f"?league={urllib.parse.quote(league)}&type={etype}")
    data = http_get_json(url)
    with open(os.path.join(RAW_DIR, f"exchange_{etype}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    core = data.get("core") or {}
    names = {}
    for it in core.get("items") or []:
        if it.get("id") and it.get("name"):
            names[str(it["id"]).lower()] = it["name"]
    rows = {}
    for ln in data.get("lines") or []:
        lid = str(ln.get("id") or "").lower()
        name = names.get(lid, ln.get("id"))
        pv = ln.get("primaryValue")
        if name and isinstance(pv, (int, float)):
            rows[str(name).lower()] = float(pv)
    return rows


def fetch_items(league, itype):
    """Stash item overview: lines carry name and primaryValue directly."""
    url = (f"{POE_NINJA_BASE}/economy/stash/current/item/overview"
           f"?league={urllib.parse.quote(league)}&type={itype}")
    data = http_get_json(url)
    with open(os.path.join(RAW_DIR, f"item_{itype}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    rows = {}
    for ln in data.get("lines") or []:
        name = ln.get("name") or ln.get("baseType")
        base = ln.get("baseType")
        pv = ln.get("primaryValue")
        if not name or not isinstance(pv, (int, float)):
            continue
        pv = float(pv)
        rows[str(name).lower()] = pv
        if base:
            rows[f"{str(name).lower()}|{str(base).lower()}"] = pv
    return rows


def db():
    conn = sqlite3.connect(PRICES_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS prices_v2
        (name TEXT, category TEXT, primary_value REAL, updated REAL,
         PRIMARY KEY (name, category))""")
    return conn


def cache_fresh(category):
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*), MAX(updated) FROM prices_v2 WHERE category=?",
        (category,)).fetchone()
    conn.close()
    return bool(row[0]) and bool(row[1]) and \
        (datetime.datetime.now().timestamp() - row[1]) < CACHE_SECONDS


def store_rows(category, rows):
    conn = db()
    ts = datetime.datetime.now().timestamp()
    for name, pv in rows.items():
        conn.execute("INSERT OR REPLACE INTO prices_v2 VALUES (?,?,?,?)",
                     (name, category, pv, ts))
    conn.commit()
    conn.close()


def load_category(category, fetch_fn):
    if cache_fresh(category):
        conn = db()
        rows = dict(conn.execute(
            "SELECT name, primary_value FROM prices_v2 WHERE category=?",
            (category,)))
        conn.close()
        if rows:
            return rows
    rows = fetch_fn()
    if rows:
        store_rows(category, rows)
    return rows


def get_leagues():
    data = http_get_json(f"{POE_NINJA_BASE}/economy/leagues")
    ids = []
    def scan(o):
        if isinstance(o, dict):
            if o.get("id"):
                ids.append(o["id"])
            for v in o.values():
                scan(v)
        elif isinstance(o, list):
            for v in o:
                scan(v)
    scan(data)
    return ids


def resolve_league(explicit):
    if explicit:
        return explicit
    env = os.environ.get("POE2_LEAGUE")
    if env:
        return env
    try:
        leagues = get_leagues()
        if leagues:
            print(f"  active leagues: {', '.join(leagues)}")
            return leagues[0]
    except Exception as exc:
        print(f"  (could not list leagues: {exc})")
    return None


def price_run(run, league):
    exchange = {}
    for t in EXCHANGE_TYPES:
        try:
            found = load_category(t, lambda t=t: fetch_exchange(league, t))
            exchange.update(found)
            print(f"  exchange/{t}: {len(found)} prices")
        except Exception as exc:
            print(f"  exchange/{t} skipped: {exc}")
    if not exchange:
        run["totals"] = {"chaos": 0.0, "divine": 0.0, "divine_in_chaos": None,
                         "error": "no exchange price data available"}
        return run

    # Exchange primaryValue is in Divine (verified: divine=1, chaos~0.099).
    # Item overview primaryValue is in Exalted (verified on UniqueArmours):
    # convert with the exchange Exalted rate before using item prices.
    pv_divine = exchange.get("divine orb")
    pv_chaos = exchange.get("chaos orb")
    pv_exalt = exchange.get("exalted orb")

    prices = dict(exchange)
    scale = pv_exalt if pv_exalt else 1.0
    for t in ITEM_TYPES:
        try:
            found = load_category("item:" + t,
                                  lambda t=t: fetch_items(league, t))
            for k, v in found.items():
                prices.setdefault(k, v * scale)
            print(f"  item/{t}: {len(found)} prices")
        except Exception as exc:
            print(f"  item/{t} skipped: {exc}")

    total_chaos = total_divine = 0.0
    matched, unmatched = [], []
    for item in run["loot"]:
        count = item.get("count", item.get("stack", 1))
        name = (item.get("name") or "").lower()
        base = (item.get("base_type") or "").lower()
        pv = None
        for key in (name, base, f"{name}|{base}"):
            if key and key in prices:
                pv = prices[key]
                break
        if pv is not None:
            divine = pv / pv_divine if pv_divine else pv
            chaos = pv / pv_chaos if pv_chaos else None
            item["divine_value"] = round(divine * count, 3)
            item["divine"] = item["divine_value"]
            item["chaos_value"] = round(chaos * count, 2) if chaos else None
            total_divine += divine * count
            total_chaos += (chaos or 0) * count
            matched.append(item)
        else:
            unmatched.append(name or base)
    run["totals"] = {
        "chaos": round(total_chaos, 2),
        "divine": round(total_divine, 3),
        "divine_in_chaos": round(pv_divine / pv_chaos, 2)
        if (pv_divine and pv_chaos) else None,
        "priced": len(matched),
        "unpriced": unmatched,
    }
    return run


# ---------------------------------------------------------------- sample

WAYSTONE_RAW = """Item Class: Waystones
Rarity: Rare
Cataclysm Path
Waystone (Tier 15)
--------
Item Rarity: 45% increased Rarity of Items found in this Area
9% more Pack Size
Monsters have 30% increased Effectiveness
Waystone Drop Chance: 50% increased chance of Waystones
Monsters deal 25% increased Damage
"""

TABLET_RAW = """Item Class: Tablets
Rarity: Magic
Breach Precursor Tablet of Havoc
--------
40% increased chance of Breaches
Monsters have 20% increased Effectiveness
"""

DIVINE_RAW = """Item Class: Stackable Currency
Rarity: Currency
Divine Orb
--------
Stack Size: 2/10
"""

EXALT_RAW = """Item Class: Stackable Currency
Rarity: Currency
Exalted Orb
--------
Stack Size: 5/20
"""

UNIQUE_RAW = """Item Class: Body Armours
Rarity: Unique
Temporalis
Silk Robe
--------
Fracturing Orb drops from this item's imagination
"""

RING_RAW = """Item Class: Rings
Rarity: Rare
Doom Coil
Amethyst Ring
--------
15% increased Rarity of Items found
+30 to maximum Life
"""


def cmd_sample():
    ensure_dirs()
    path = os.path.join(SAMPLE_DIR, "run_SAMPLE.jsonl")

    def ev(**kw):
        return json.dumps(kw, ensure_ascii=False)

    lines = [
        ev(t="2026-10-29T20:00:00", type="item", role="waystone_used",
           item_class="Waystones", rarity="Rare", base_type="Waystone (Tier 15)",
           name="Cataclysm Path", waystone_tier=15, raw=WAYSTONE_RAW),
        ev(t="2026-10-29T20:00:10", type="item", role="tablet_used",
           item_class="Tablets", rarity="Magic",
           base_type="Breach Precursor Tablet",
           name="Breach Precursor Tablet of Havoc", raw=TABLET_RAW),
        ev(t="2026-10-29T20:00:20", type="item", role="tablet_used",
           item_class="Tablets", rarity="Normal",
           base_type="Irradiated Tablet", raw=TABLET_RAW),
        ev(t="2026-10-29T20:01:00", type="item", role="gear",
           item_class="Rings", rarity="Rare", base_type="Amethyst Ring",
           name="Doom Coil", iir=15, raw=RING_RAW),
        ev(t="2026-10-29T20:01:30", type="gear_total", iir=15, items=1),
        ev(t="2026-10-29T20:05:00", type="item", role="loot",
           item_class="Stackable Currency", rarity="Currency",
           base_type="Exalted Orb", name="Exalted Orb", raw=EXALT_RAW),
        ev(t="2026-10-29T20:08:00", type="item", role="loot",
           item_class="Stackable Currency", rarity="Currency",
           base_type="Divine Orb", name="Divine Orb", raw=DIVINE_RAW),
        ev(t="2026-10-29T20:09:00", type="item", role="loot",
           item_class="Body Armours", rarity="Unique",
           base_type="Silk Robe", name="Temporalis", raw=UNIQUE_RAW),
        ev(t="2026-10-29T20:10:00", type="note",
           text="savannah, desert biome, 8 min, 0 deaths, juiced with 3 paranoia"),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"Sample run written: {path}")
    print("Test the full pipeline with:")
    print(f"  python3 {os.path.basename(__file__)} report {path}")


# ---------------------------------------------------------------- report

def cmd_report(path, league=None, model=DEFAULT_MODEL):
    ensure_dirs()
    run = parse_run(path)
    run["patch"] = DEFAULT_PATCH
    league = resolve_league(league)
    if league:
        run["league"] = league
        price_run(run, league)
    else:
        print("  (no league - pricing skipped)")
    session = parse_notes(run, model)
    if session:
        dm = session.get("duration_min")
        if isinstance(dm, str) and dm.strip().isdigit():
            session["duration_min"] = float(dm)
        run["session"] = session
        biome = session.get("biome")
        if biome and biome != "unknown":
            run["biome"] = biome
    stem = os.path.splitext(os.path.basename(path))[0]
    out_path = os.path.join(PARSED_DIR, f"{stem}_enriched.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(run, fh, indent=2, ensure_ascii=False)
    print(f"\nEnriched run saved: {out_path}")
    ws = run.get("waystone_used") or {}
    print(f"  Waystone : T{ws.get('tier', '?')} "
          f"({len(ws.get('mods', []))} mods, bonuses {ws.get('bonuses')})")
    print(f"  Tablets  : "
          + (", ".join(t.get("base_type", "?")
                        for t in run["tablets_used"]) or "none"))
    print(f"  Gear IIR : +{run.get('gear_iir')}%")
    if run.get("session"):
        print(f"  Session  : {run['session'].get('biome')} / "
              f"{run['session'].get('map_name')} / "
              f"{run['session'].get('duration_min')} min / "
              f"{run['session'].get('deaths')} deaths")
    t = run.get("totals", {})
    print(f"  Loot     : {len(run['loot'])} items, "
          f"worth ~{t.get('divine', 0)} div "
          f"(~{t.get('chaos', 0)} chaos)")
    if t.get("unpriced"):
        print(f"  Unpriced : {', '.join(t['unpriced'][:10])}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "sample":
        cmd_sample()
    elif cmd == "leagues":
        print("Active leagues:", ", ".join(get_leagues()))
    elif cmd == "parse" and len(sys.argv) > 2:
        ensure_dirs()
        print(json.dumps(parse_run(sys.argv[2]), indent=2, ensure_ascii=False))
    elif cmd == "raw" and len(sys.argv) > 3:
        ensure_dirs()
        league, cat = sys.argv[2], sys.argv[3]
        if cat.startswith("item:"):
            rows = fetch_items(league, cat.split(":", 1)[1])
        else:
            rows = fetch_exchange(league, cat)
        print(f"{len(rows)} price rows; raw JSON saved under {RAW_DIR}")
    elif cmd in ("notes", "price", "report") and len(sys.argv) > 2:
        args = sys.argv[2:]
        runfile = args[0]
        flags = {}
        for i, a in enumerate(args[1:], 1):
            if a == "--league" and i + 1 < len(args):
                flags["league"] = args[i + 1]
        ensure_dirs()
        run = parse_run(runfile)
        if cmd == "notes":
            print(json.dumps(parse_notes(run, DEFAULT_MODEL),
                            indent=2, ensure_ascii=False))
        elif cmd == "price":
            league = resolve_league(flags.get("league"))
            if not league:
                sys.exit("No league available; use --league or POE2_LEAGUE")
            price_run(run, league)
            print(json.dumps(run, indent=2, ensure_ascii=False))
        else:
            cmd_report(runfile, flags.get("league"))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
