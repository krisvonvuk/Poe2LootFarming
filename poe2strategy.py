#!/usr/bin/env python3
"""poe2strategy - deterministic loot-target strategy recommender for PoE2 (0.5.x).

Answers: "I want loot X on my build - what waystone bonuses, tablet combo,
biome and atlas nodes should I use?" and "what do MY runs actually earn?"

Game-data corrections vs earlier drafts (verified against 0.5.x data exports):
- Waystones have NO "Increased Quantity of Items found" line. Every waystone
  mod pays exactly one bonus: Item Rarity, Waystone Drop Chance, Pack Size,
  Monster Rarity, or Monster Effectiveness (the last is also the danger tax).
- Tablets are slotted in the map device WITH the waystone (up to 3, or 4 on
  City-biome maps with Industrial Improvements). A run uses a tablet COMBO,
  not one tablet, so run files store a list.

Commands:
  targets                       list known loot targets
  recommend "<words>" [--build squishy|balanced|tanky] [--tier N]
  ask "<free text>"             local LLM extracts target/build/tier, then
                                delegates to the deterministic engine
  stats                         measured div/h per (biome, tablet combo)
  chart [--since YYYY-MM-DD] [--patch TAG]  ASCII charts: div/h per combo,
                                most common drops, best/worst combos
  check [--build squishy]       inspect each run's ACTUAL waystone mods
                                against the build's danger-mod list

Files it reads (produced by your run logger / poe2agent.py):
  data/parsed/run_*_enriched.json - one per run. Expected fields:
    {
      "patch": "0.5.5",
      "biome": "Desert",
      "waystone_used": {"tier": 15,
                        "bonuses": {"rarity": 12, "pack_size": 20,
                                    "monster_rarity": 36,
                                    "effectiveness": 13,
                                    "waystone_drop": 50},
                        "mods": ["...raw text..."]},
      "tablets_used": [{"base_type": "Breach Precursor Tablet",
                        "mods": ["..."]},
                       {"base_type": "Irradiated Tablet", "mods": [...]}],
      "loot": [{"name": "Divine Orb", "count": 2, "divine": 2.0}, ...],
      "totals": {"divine": 3.4},
      "session": {"duration_min": 8}
    }
  Older single-tablet files ("tablet_used") are still accepted, but
  until poe2agent.py emits the full format, biome will show as "unknown
  biome", --patch/--since filters exclude runs missing those fields, and
  the "most common drops" chart stays empty. Enricher TODO list:
    - tablets_used: list (not single tablet_used)
    - biome: from run notes
    - patch: e.g. "0.5.5", set once per patch
    - started_at: ISO timestamp (needed by --since)
    - loot: [{"name": ..., "count": N}] per item, from captured drops
    - waystone_used.mods: raw modifier text lines (check command reads these)

  strategies.json (optional, same dir as this script) is merged over the
  built-in TARGETS dict: edit or add per-target entries without code
  changes; unknown keys add new targets.

Environment:
  OLLAMA_URL   (default http://localhost:11434)
  OLLAMA_MODEL (default qwen3:8b)

Examples:
  python3 poe2strategy.py recommend "essences" --build squishy --tier 15
  python3 poe2strategy.py stats
  python3 poe2strategy.py chart --patch 0.5.5

This tool is a personal, non-commercial helper. It runs completely
separate from the game, reads only the clipboard you explicitly copy
to, sends no input to the game, and reads no game files or memory.
This product isn't affiliated with or endorsed by Grinding Gear
Games in any way.
"""

import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARSED_DIR = os.path.join(BASE_DIR, "data", "parsed")
DEFAULT_PATCH = "0.5.5"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

# The only bonuses a waystone mod can pay (0.5.x). No quantity line exists.
WAYSTONE_BONUSES = ["rarity", "pack_size", "monster_rarity",
                    "effectiveness", "waystone_drop"]

# --------------------------------------------------------------------------
# Build profiles: waystone danger keywords to reroll per archetype.

BUILD_PROFILES = {
    "squishy": [
        "maximum Elemental Resistances",
        "increased Damage",
        "Extra Elemental Damage",
        "Extra Fire Damage",
        "Extra Cold Damage",
        "Extra Lightning Damage",
        "less Armour",
        "less Evasion",
        "Cannot Regenerate",
        "Cannot Leech",
    ],
    "balanced": [
        "maximum Elemental Resistances",
        "Cannot Regenerate",
    ],
    "tanky": [],
}

# --------------------------------------------------------------------------
# Loot targets. tablet_family is a machine-readable key used for the
# measured-stats tie-in (never parsed from prose). prefixes are priority-
# ordered waystone bonuses from WAYSTONE_BONUSES only.

TARGETS = {
    "raw_currency": {
        "label": "Raw currency (Exalted/Divine/Chaos)",
        "keywords": ["exalted", "divine", "chaos", "currency", "div",
                     "gold", "money"],
        "tablet_family": ["irradiated", "breach", "delirium"],
        "tablet": "Irradiated (generic pack size / monster rarity juice) "
                  "or a density mechanic: Breach or Delirium Precursor "
                  "Tablet. Combine 2-3 tablets per map - tablet slots come "
                  "from using a rare waystone.",
        "biome": "Desert (rare-monster and drop-quantity nodes); any biome "
                 "works, favour dense open maps",
        "atlas": ["Generic loot nodes: increased Rare/Magic Monsters, Item "
                  "Rarity",
                  "Desert Biome: Drop Quantity / Rare Monster nodes",
                  "Enigmatic Intensification (waystone modifier effect per "
                  "tablet slotted)"],
        "prefixes": ["pack_size", "rarity", "monster_rarity"],
        "emotions": ["Greed (item rarity)", "Paranoia x3 (rare monsters)"],
        "omens": ["Omen of Chaotic Rarity / Omen of Chaotic Monsters to "
                  "steer Chaos-rolled bonuses"],
        "notes": "Rarity shifts rare-currency weighting; pack size scales "
                 "volume. MF gear 100-150% if the build survives it.",
    },
    "waystone_sustain": {
        "label": "Waystone sustain",
        "keywords": ["waystone", "sustain", "maps", "stones"],
        "tablet_family": ["overseer"],
        "tablet": "Overseer (extra map bosses = tier+1 waystone drops); "
                  "surplus tablets with good mods sell",
        "biome": "Any; always kill the final boss",
        "atlas": ["Pathkeepers / Archaeological Interest / Valuable Paths",
                  "Atop the World (boss waystone drops)"],
        "prefixes": ["waystone_drop", "rarity"],
        "emotions": [],
        "omens": [],
        "notes": "Keep 3-4 mods if you need revives; 6 mods = 0 revives. "
                 "Skipping bosses is the main cause of map starvation.",
    },
    "essences": {
        "label": "Essences",
        "keywords": ["essence", "essences", "whispering", "screaming"],
        "tablet_family": ["irradiated"],
        "tablet": "Irradiated / any density tablet; essences spawn on rare "
                  "monsters",
        "biome": "Desert (Desert Biome: Essence Chance node)",
        "atlas": ["Desert Biome: Essence Chance",
                  "Generic rare-monster nodes",
                  "Corrupted Lattices keystone if you can clear corrupted "
                  "packs"],
        "prefixes": ["monster_rarity", "pack_size", "rarity"],
        "emotions": ["Paranoia x3"],
        "omens": [],
        "notes": "Essence chance is Desert-atlas-gated; without the node "
                 "you rely on random spawns.",
    },
    "breach": {
        "label": "Breach (Catalysts, Splinters, Xesht)",
        "keywords": ["breach", "catalyst", "splinter", "xesht", "clasp"],
        "tablet_family": ["breach"],
        "tablet": "Breach Precursor Tablet; stack 2-3 in one map for "
                  "overlapping juice, or 1 Breach + 1 Irradiated",
        "biome": "Any open/linear map; Breach needs space",
        "atlas": ["Breach branch nodes",
                  "Tear Open the Rift keystone",
                  "Breeding Program keystone if running Breach Hives"],
        "prefixes": ["pack_size", "monster_rarity", "waystone_drop"],
        "emotions": ["Paranoia x3"],
        "omens": [],
        "notes": "Clear edge-to-centre, kill stabilising rares. Catalysts "
                 "are Breach-only drops.",
    },
    "delirium": {
        "label": "Delirium (Splinters, Emotions, Simulacrum)",
        "keywords": ["delirium", "simulacrum", "emotion", "distilled",
                     "mists"],
        "tablet_family": ["delirium"],
        "tablet": "Delirium Precursor Tablet; 3 Delirium + 1 Overseer is a "
                  "common City-biome setup (4th slot via Industrial "
                  "Improvements)",
        "biome": "Any; Delirium scales with total map clear",
        "atlas": ["Delirium branch nodes",
                  "The Chosen Path keystone set to Delirium"],
        "prefixes": ["pack_size", "rarity", "monster_rarity"],
        "emotions": ["Greed", "Paranoia x3"],
        "omens": [],
        "notes": "Delirious % scales splinter and emotion drops; full-clear "
                 "every map.",
    },
    "ritual": {
        "label": "Ritual (Tribute, Omens, Idols)",
        "keywords": ["ritual", "tribute", "idol"],
        "tablet_family": ["ritual"],
        "tablet": "Ritual Precursor Tablet (extra Rituals / rerolls / "
                  "Tribute)",
        "biome": "Any",
        "atlas": ["Ritual branch nodes (extra Rituals, Tribute quantity)",
                  "Traveller's Woe keystone"],
        "prefixes": ["pack_size", "rarity"],
        "emotions": [],
        "omens": ["Omens come FROM Ritual: buy crafting omens with Tribute"],
        "notes": "Deferred rewards and rerolls compound Tribute value.",
    },
    "expedition": {
        "label": "Expedition (Logbooks, currency, artifacts)",
        "keywords": ["expedition", "logbook", "verisium", "artifact",
                     "explosive"],
        "tablet_family": ["expedition"],
        "tablet": "Expedition Precursor Tablet (extra Explosives, "
                  "remnants, mutation chance)",
        "biome": "Any; Cultivate the Sea treats Ocean areas as a second "
                 "biome",
        "atlas": ["Expedition branch",
                  "Calculated Investment keystone",
                  "Steady Development / Strategic Advantage"],
        "prefixes": ["pack_size", "rarity"],
        "emotions": [],
        "omens": [],
        "notes": "Logbooks scale with number of digs, not map rarity; "
                 "density is king.",
    },
    "abyss": {
        "label": "Abyss (Abyssal Bones, socketables)",
        "keywords": ["abyss", "abyssal", "fracturing", "socketable"],
        "tablet_family": ["abyss"],
        "tablet": "Abyss Tablet (extra Abysses, additional pits)",
        "biome": "Any; open layouts so pits spawn cleanly",
        "atlas": ["Abyss branch nodes",
                  "Vile Treasures keystone",
                  "Balance of Power keystone (choose Abyss faction)"],
        "prefixes": ["pack_size", "monster_rarity"],
        "emotions": [],
        "omens": [],
        "notes": "Fracturing Orb drops from The Immured Fury in Corrupted "
                 "Nexuses - corrupt 4+ mod waystones to spawn Nexuses.",
    },
    "rogue_exiles": {
        "label": "Rogue Exiles (uniques, extra waystones)",
        "keywords": ["rogue exile", "rogue exiles"],
        "tablet_family": ["irradiated"],
        "tablet": "Irradiated / any density tablet",
        "biome": "City (City Biome: Rogue Exile Chance node)",
        "atlas": ["City Biome: Rogue Exile Chance",
                  "Well-equipped Opponents keystone",
                  "Legendary Duels keystone",
                  "Competing Explorers (extra waystone from exiles)"],
        "prefixes": ["pack_size", "rarity"],
        "emotions": [],
        "omens": [],
        "notes": "Exiles scale with player MF; densest humanoid loot "
                 "source in City maps.",
    },
    "uniques_mf": {
        "label": "Unique items / magic find",
        "keywords": ["unique", "uniques", "magic find", "mf",
                     "temporalis"],
        "tablet_family": ["delirium", "breach", "irradiated"],
        "tablet": "Delirium or Breach (density multiplies MF rolls), or "
                  "boss-targeting with Overseer",
        "biome": "Any; chase uniques are boss-gated - check the boss loot "
                 "table first",
        "atlas": ["Generic Rarity nodes",
                  "Boss branch nodes for boss-gated targets",
                  "The Journey Ahead keystone set to Rarity"],
        "prefixes": ["rarity", "pack_size", "monster_rarity"],
        "emotions": ["Greed"],
        "omens": ["Omen of Chaotic Rarity to force rarity bonuses"],
        "notes": "MF builds run 100-150% Increased Rarity on gear; rare-"
                 "monster density compounds with MF.",
    },
    "experience": {
        "label": "Experience / levelling",
        "keywords": ["experience", "xp", "levelling", "leveling"],
        "tablet_family": ["breach"],
        "tablet": "Breach Precursor Tablet (highest monster density)",
        "biome": "Desert (Desert Biome: Experience node)",
        "atlas": ["Generic pack-size and rare-monster nodes",
                  "Calculated Investment: Experience"],
        "prefixes": ["pack_size", "effectiveness"],
        "emotions": ["Paranoia x3"],
        "omens": [],
        "notes": "Effectiveness is the XP lever: tougher monsters pay more "
                 "XP and more drops. Run only what your build survives.",
    },
    "lineage_gems": {
        "label": "Lineage Support Gems / boss chase items",
        "keywords": ["lineage", "support gem", "relic", "boss unique"],
        "tablet_family": ["overseer"],
        "tablet": "Overseer (extra bosses) or target the specific boss",
        "biome": "Any",
        "atlas": ["Boss branch (difficulty nodes raise Rare/Very Rare "
                  "odds)",
                  "Overlord's Domain / Overlord's Influence"],
        "prefixes": ["rarity", "pack_size"],
        "emotions": [],
        "omens": [],
        "notes": "Lineage gems are boss-pool gated; no map mod substitutes "
                 "for pool access.",
    },
    "soul_cores": {
        "label": "Soul Cores / Vaal content",
        "keywords": ["soul core", "vaal", "vault", "atziri"],
        "tablet_family": ["temple"],
        "tablet": "Temple Tablet / any",
        "biome": "Vaal City biome maps for temple access",
        "atlas": ["Incursion/Vaal branch nodes",
                  "Atziri's Assets keystone"],
        "prefixes": ["rarity", "pack_size"],
        "emotions": [],
        "omens": [],
        "notes": "Soul Cores trade on the exchange; temple runs and vaults "
                 "are the source.",
    },
}

BONUS_LABELS = {
    "rarity": "increased Rarity of Items found",
    "pack_size": "more Pack Size",
    "monster_rarity": "increased Monster/Rare Monsters",
    "effectiveness": "Monster Effectiveness (danger tax: also XP+quantity)",
    "waystone_drop": "Waystone Drop Chance",
}


def load_targets():
    """Built-in TARGETS, optionally overridden by strategies.json."""
    targets = TARGETS
    path = os.path.join(BASE_DIR, "strategies.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                override = json.load(fh)
            if isinstance(override, dict) and override:
                for tid, t in override.items():
                    if isinstance(t, dict):
                        merged = dict(targets.get(tid, {}))
                        merged.update(t)
                        targets[tid] = merged
        except (OSError, json.JSONDecodeError) as e:
            print(f"warning: strategies.json unreadable ({e}); "
                  "using built-ins", file=sys.stderr)
    return targets


# --------------------------------------------------------------------------
# Matching

def match_target(words, targets):
    words = words.lower()
    best, best_score = None, 0
    for tid, t in targets.items():
        score = sum(1 for kw in t.get("keywords", [])
                    if kw.lower() in words)
        if score > best_score:
            best, best_score = tid, score
    return best, best_score


def flagged_mods(mods, build):
    avoid = BUILD_PROFILES.get(build, [])
    flagged = [m for m in mods
               if any(a.lower() in m.lower() for a in avoid)]
    return flagged


# --------------------------------------------------------------------------
# Run loading / normalisation

def normalise_run(raw):
    """Accept old single-tablet and new multi-tablet run formats."""
    r = dict(raw)
    tablets = r.get("tablets_used")
    if not tablets and r.get("tablet_used"):
        tablets = [r["tablet_used"]]
    r["tablets_used"] = tablets or []
    r.setdefault("patch", None)
    r.setdefault("biome", None)
    r.setdefault("loot", [])
    r.setdefault("totals", {})
    r.setdefault("session", {})
    return r


def load_runs(patch=None, since=None):
    runs = []
    if not os.path.isdir(PARSED_DIR):
        return runs
    for fn in sorted(os.listdir(PARSED_DIR)):
        if not fn.endswith("_enriched.json"):
            continue
        try:
            with open(os.path.join(PARSED_DIR, fn),
                      encoding="utf-8") as fh:
                r = normalise_run(json.load(fh))
        except (OSError, json.JSONDecodeError):
            continue
        if patch and r.get("patch") != patch:
            continue
        if since:
            ts = r.get("started_at") or r.get("timestamp") or ""
            if not ts or ts < since:
                continue
        runs.append(r)
    return runs


def tablet_key(r):
    """Canonical tablet-combination key, e.g. 'breach x2 + irradiated'."""
    fams = sorted(tablet_family(t) for t in r["tablets_used"]
                  if tablet_family(t))
    if not fams:
        return "no tablet"
    counts = {}
    for f in fams:
        counts[f] = counts.get(f, 0) + 1
    return " + ".join(f"{f} x{n}" if n > 1 else f
                      for f, n in sorted(counts.items()))


def tablet_family(tablet):
    bt = (tablet or {}).get("base_type", "") or ""
    bt = bt.lower()
    for fam in ("breach", "delirium", "ritual", "expedition", "abyss",
                "overseer", "irradiated", "temple"):
        if fam in bt:
            return fam
    return "other" if bt else ""


def duration_ok(r):
    m = r["session"].get("duration_min")
    return isinstance(m, (int, float)) and m > 0


def div_hour(r):
    div = r["totals"].get("divine") or 0.0
    return div / (r["session"]["duration_min"] / 60.0)


def combo_rows(runs):
    """Group runs by (biome, tablet combo). Returns {key: [div/h,...]}."""
    groups = defaultdict(list)
    for r in runs:
        if not duration_ok(r):
            continue
        key = (r.get("biome") or "unknown biome", tablet_key(r))
        groups[key].append(div_hour(r))
    return groups


def drop_counts(runs):
    counts = defaultdict(int)
    for r in runs:
        for item in r["loot"]:
            name = (item.get("name") or item.get("raw") or "?").strip()
            counts[name] += item.get("count", 1)
    return counts


# --------------------------------------------------------------------------
# Measured statistics

def print_stats(runs, patch=None):
    if not runs:
        print("No enriched runs in data/parsed/ yet.")
        return
    skipped = sum(1 for r in runs if not duration_ok(r))
    groups = combo_rows(runs)
    if not groups:
        print("Runs exist but none have a parseable duration_min; "
              "write 'N min' in your run notes.")
        return
    tag = f" (patch {patch})" if patch else ""
    print(f"{len(runs)} enriched runs{tag}, {skipped} skipped "
          f"(no duration)\n")
    ranked = sorted(groups.items(),
                    key=lambda kv: -(sum(kv[1]) / len(kv[1])))
    for (biome, combo), rates in ranked:
        avg = sum(rates) / len(rates)
        print(f"  {biome:12s} | {combo:35s} {len(rates):3d} runs   "
              f"avg {avg:6.2f} div/h")
    thin = [k for k, v in groups.items() if len(v) < 5]
    if thin:
        print(f"\nLow sample (<5 runs) for {len(thin)} combo(s): treat "
              "their div/h as noise.")
    if len(runs) < 30:
        print("Overall sample is small: 30+ runs before trusting "
              "rankings.")


def print_chart(runs, patch=None, since=None):
    if not runs:
        print("No enriched runs yet - nothing to chart.")
        return
    groups = combo_rows(runs)
    if groups:
        print("Div/hour per (biome, tablet combo)  "
              f"[{len(runs)} runs]\n")
        ranked = sorted(groups.items(),
                        key=lambda kv: -(sum(kv[1]) / len(kv[1])))
        longest = max((len(f"{b} | {c}")
                      for (b, c) in groups) or 1)
        scale = 40.0 / max(max(sum(v) / len(v) for v in groups.values()),
                           1e-9)
        for (biome, combo), rates in ranked:
            avg = sum(rates) / len(rates)
            bar = "#" * max(1, int(avg * scale))
            print(f"  {f'{biome} | {combo}':<{longest}}  "
                  f"{avg:6.2f}  {bar}")
        best = ranked[0]
        worst = ranked[-1]
        print(f"\nMost profitable : {best[0][0]} | {best[0][1]} "
              f"({sum(best[1]) / len(best[1]):.2f} div/h)")
        print(f"Least profitable: {worst[0][0]} | {worst[0][1]} "
              f"({sum(worst[1]) / len(worst[1]):.2f} div/h)")
    counts = drop_counts(runs)
    if counts:
        print("\nMost common drops:\n")
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:15]
        width = max(len(n) for n, _ in top)
        scale = 40.0 / max(top[0][1], 1)
        for name, n in top:
            print(f"  {name:<{width}}  {n:5d}  "
                  f"{'#' * max(1, int(n * scale))}")


# --------------------------------------------------------------------------
# Recommend

def cmd_recommend(words, build="balanced", tier=None, tid=None,
                  targets=None, runs=None):
    targets = targets or load_targets()
    runs = runs if runs is not None else load_runs()
    if tid is None:
        tid, score = match_target(words, targets)
    if tid not in targets:
        print(f"No target matched '{words}'. Run 'targets' for the list.")
        return
    t = targets[tid]

    # tier wiring: effectiveness danger and bonus expectations scale
    tier_note = ""
    if tier:
        if not isinstance(tier, int) or not 1 <= tier <= 16:
            tier = None
        else:
            if tier >= 15:
                tier_note = ("\nHigh tier: monster Effectiveness mods hit "
                             "hard; a squishy build should reroll them "
                             "even if it costs XP throughput.")
            if tier <= 5:
                tier_note = ("\nLow tier: waystone sustain matters more "
                             "than juice - keep Waystone Drop Chance and "
                             "kill the boss.")
    print(f"Strategy: {t['label']}   (build: {build}"
          + (f", tier {tier}" if tier else "") + ")\n")
    print(f"Tablets:\n  {t['tablet']}")
    print(f"Biome / maps:\n  {t['biome']}")
    if t.get("atlas"):
        print("Atlas nodes:")
        for a in t["atlas"]:
            print(f"  - {a}")
    if t.get("prefixes"):
        print("Waystone bonuses to roll, priority order:")
        for p in t["prefixes"]:
            print(f"  - {BONUS_LABELS.get(p, p)}")
    for key, title in (("emotions", "Distilled Emotions"),
                       ("omens", "Omens / crafting")):
        if t.get(key):
            print(f"{title}:\n  " + "; ".join(t[key]))
    if t.get("notes"):
        print(f"Notes:\n  {t['notes']}")
    if tier_note:
        print(tier_note)

    avoid = BUILD_PROFILES.get(build, [])
    if avoid:
        print(f"\nDanger mods to reroll for a {build} build:")
        for a in avoid:
            print(f"  - {a}")

    # measured tie-in: match on tablet_family, never on prose
    families = set(t.get("tablet_family") or [])
    rows, n_runs = [], 0
    for r in runs:
        fams = {tablet_family(x) for x in r["tablets_used"]}
        if families & fams and duration_ok(r):
            rows.append(div_hour(r))
            n_runs += 1
    if rows:
        print(f"\nYour measured runs using these tablet families: "
              f"{n_runs} runs, avg {sum(rows) / len(rows):.2f} div/h")
    else:
        print("\nNo measured runs for this strategy yet - log some and "
              "this line will show your actual div/h.")


def cmd_check(runs, build):
    """Wire flagged_mods: inspect each run's actual waystone mods
    against the build profile's danger keywords."""
    avoid = BUILD_PROFILES.get(build, [])
    if not avoid:
        print(f"A {build} build has no danger-mod restrictions; "
              "nothing to check.")
        return
    checked = 0
    for r in runs:
        ws = r.get("waystone_used") or {}
        mods = ws.get("mods") or []
        if not mods:
            continue
        checked += 1
        flagged = flagged_mods(mods, build)
        if flagged:
            tier = ws.get("tier", "?")
            combo = tablet_key(r)
            biome = r.get("biome") or "unknown biome"
            print(f"DANGER for a {build} build - tier {tier} waystone, "
                  f"{biome}, {combo}:")
            for m in flagged:
                print(f"  ! {m}")
    if checked == 0:
        print("No runs with waystone mods found in data/parsed/.")
    else:
        print(f"\nChecked {checked} run(s) against the {build} danger "
              "list.")


def cmd_targets(targets):
    for tid, t in targets.items():
        print(f"{tid:15s} {t.get('label', tid)}")


# --------------------------------------------------------------------------
# Optional LLM front end

def ollama_parse_intent(text, targets):
    system = (
        "You convert a Path of Exile 2 player's request into JSON. "
        "Output ONLY a JSON object with these keys: "
        '"target" - exactly one of: '
        + ", ".join(sorted(targets)) + "; "
        '"build" - exactly one of: ' + ", ".join(sorted(BUILD_PROFILES))
        + " (fragile/crit/low-life/squishy -> squishy, tanky/defensive "
          "-> tanky, otherwise balanced); "
        '"tier" - map tier 1-16 as an integer, or null if not mentioned. '
        "No other keys, no explanations, no markdown."
    )
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "format": "json",
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        OLLAMA_URL.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data.get("message", {}).get("content", "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"{.*}", content, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def cmd_ask(text, targets):
    try:
        intent = ollama_parse_intent(text, targets)
    except Exception as e:
        sys.exit(f"Ollama unreachable ({e}). Use "
                 "'recommend \"<keywords>\" --build <profile> --tier <n>' "
                 "instead - the deterministic engine needs no LLM.")
    if not intent or intent.get("target") not in targets:
        sys.exit("LLM could not map the request to a known target. "
                 "Run 'targets' and use 'recommend \"<keywords>\"'.")
    build = intent.get("build")
    if build not in BUILD_PROFILES:
        build = "balanced"
    tier = intent.get("tier")
    if not isinstance(tier, int) or not 1 <= tier <= 16:
        tier = None
    print(f"Understood as: target={intent['target']}, build={build}"
          + (f", tier={tier}" if tier else "") + "\n")
    cmd_recommend(None, build, tier, tid=intent["target"],
                  targets=targets)


# --------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    flags, pos = {}, []
    i = 0
    while i < len(args):
        if args[i] in ("--build", "--tier", "--patch", "--since") \
                and i + 1 < len(args):
            flags[args[i][2:]] = args[i + 1]
            i += 2
        else:
            pos.append(args[i])
            i += 1
    targets = load_targets()
    patch = flags.get("patch")
    since = flags.get("since")
    runs = load_runs(patch=patch, since=since)

    if cmd == "targets":
        cmd_targets(targets)
    elif cmd == "recommend" and pos:
        build = flags.get("build", "balanced")
        if build not in BUILD_PROFILES:
            sys.exit(f"build must be one of: {', '.join(BUILD_PROFILES)}")
        tier = flags.get("tier")
        if tier is not None:
            try:
                tier = int(tier)
            except ValueError:
                sys.exit("--tier must be an integer 1-16")
        cmd_recommend(" ".join(pos), build, tier, targets=targets,
                      runs=runs)
    elif cmd == "ask" and pos:
        cmd_ask(" ".join(pos), targets)
    elif cmd == "stats":
        print_stats(runs, patch=patch)
    elif cmd == "chart":
        print_chart(runs, patch=patch, since=since)
    elif cmd == "check":
        build = flags.get("build", "balanced")
        if build not in BUILD_PROFILES:
            sys.exit(f"build must be one of: {', '.join(BUILD_PROFILES)}")
        cmd_check(runs, build)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
