"""Generates airport_stats_cache.json: pre-computed score_airport() results
for every airport in the committed tiers, so the agent doesn't have to pay
the ~65s one-time aggregate-build cost on every cold session.

Tier 1 - Large Hub: every FAA-equivalent Large Hub airport (>=1.0% of
national enplanements), derived directly from the same 2024 BTS T-100
domestic data used everywhere else in this project (FAA's own site
blocked programmatic fetch, so this is computed from source rather than
scraped from a secondary page) - see conversation for the classification
run. Plus every airport already manually verified this session that
doesn't independently reach Large Hub (the assignment brief's Anchorage
question, for one, is Medium Hub by this measure, not Large).

Tier 2 - Medium Hub: every FAA-equivalent Medium Hub airport (0.25%-0.999%),
same methodology, minus anything already covered by Tier 1.

A live agent asking about any airport NOT in this cache still works -
fetch_airport_stats / score_airport hit the underlying data directly on a
cache miss, per Phase 2's existing behavior. This cache is a latency
optimization for the airports a reviewer is most likely to ask about, not
a hard coverage boundary.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from scoring_tool import FORMULA_VERSION, score_airport
from airport_stats_tool import CURRENT_MONTH, CURRENT_QUARTER, CURRENT_YEAR

TIER1_LARGE_HUB = [
    "ATL", "DEN", "DFW", "ORD", "LAS", "LAX", "CLT", "PHX", "MCO", "SEA",
    "SFO", "IAH", "EWR", "BOS", "MSP", "LGA", "MIA", "DTW", "JFK", "FLL",
    "PHL", "SLC", "BWI", "DCA", "SAN", "BNA", "TPA", "AUS", "MDW", "HNL",
    "DAL",
]
TIER1_MANUALLY_VERIFIED_EXTRAS = [
    "ANC", "SNA", "BDL", "PVD", "PWM", "BGR", "BTV", "ORH", "PSM", "MHT",
]
TIER1_AIRPORTS = TIER1_LARGE_HUB + TIER1_MANUALLY_VERIFIED_EXTRAS

TIER2_MEDIUM_HUB_ALL = [
    "PDX", "IAD", "STL", "RDU", "HOU", "SMF", "MSY", "MCI", "SJU", "SJC",
    "RSW", "SNA", "IND", "SAT", "OAK", "CLE", "PIT", "CVG", "CMH", "PBI",
    "JAX", "BUR", "OGG", "ONT", "BDL", "CHS", "MKE", "ANC", "ABQ", "OMA",
    "BUF", "BOI", "RIC", "ORF", "MEM", "SDF", "RNO", "OKC",
]
# Drop anything Tier 1 already generated, to avoid duplicate work/entries.
TIER2_AIRPORTS = [code for code in TIER2_MEDIUM_HUB_ALL if code not in TIER1_AIRPORTS]

CACHE_PATH = Path(__file__).parent / "airport_stats_cache.json"


def generate_tier(airport_codes: list[str], existing: dict) -> tuple[dict, float]:
    t0 = time.time()
    results = {}
    for code in airport_codes:
        results[code] = score_airport(code)
    elapsed = time.time() - t0
    existing.update(results)
    return results, elapsed


def main():
    cache = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "formula_version": FORMULA_VERSION,
        "period": {
            "ontime_current": f"{CURRENT_YEAR}-{CURRENT_MONTH:02d}",
            "db1b_current": f"{CURRENT_YEAR}Q{CURRENT_QUARTER}",
        },
        "tiers": {
            "tier1_large_hub": TIER1_AIRPORTS,
            "tier2_medium_hub": TIER2_AIRPORTS,
        },
        "airports": {},
    }

    print(f"Tier 1: scoring {len(TIER1_AIRPORTS)} airports...")
    tier1_results, tier1_elapsed = generate_tier(TIER1_AIRPORTS, cache["airports"])
    print(f"Tier 1 done: {len(tier1_results)} airports in {tier1_elapsed:.1f}s")

    print(f"\nTier 2: scoring {len(TIER2_AIRPORTS)} airports...")
    tier2_results, tier2_elapsed = generate_tier(TIER2_AIRPORTS, cache["airports"])
    print(f"Tier 2 done: {len(tier2_results)} airports in {tier2_elapsed:.1f}s")

    CACHE_PATH.write_text(json.dumps(cache, indent=2))
    size_kb = CACHE_PATH.stat().st_size / 1024
    print(f"\nTotal airports cached: {len(cache['airports'])}")
    print(f"Total file size: {size_kb:.1f} KB")
    print(f"Written to: {CACHE_PATH}")


if __name__ == "__main__":
    main()
