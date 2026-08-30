"""Generates region_airports_cache.json: a precomputed US Census region ->
airport-codes mapping for the default period.

Real bug this closes: airports_in_region() (used by rank_airports_in_region)
answers "which airport codes are in this region" by scanning the raw
on-time-performance file's state column - a step airport_stats_cache.json's
per-airport cache never covered, since that cache only accelerates looking
up stats for an airport you already have the code for. On a cold
data_cache/, that scan alone costs a full BTS file download+parse
(measured: ~54s on one run) before any per-airport scoring even starts.
This file removes that cost for the named Census regions the app actually
supports.
"""

import json
from pathlib import Path

from airport_stats_tool import CURRENT_MONTH, CURRENT_YEAR, airports_in_region
from scoring_tool import REGION_STATES

CACHE_PATH = Path(__file__).parent / "region_airports_cache.json"


def main():
    regions = {}
    for name, states in REGION_STATES.items():
        codes = sorted(airports_in_region(states, CURRENT_YEAR, CURRENT_MONTH))
        regions[name] = codes
        print(f"{name}: {len(codes)} airports")

    data = {
        "period": f"{CURRENT_YEAR}-{CURRENT_MONTH:02d}",
        "regions": regions,
    }
    CACHE_PATH.write_text(json.dumps(data, indent=2))
    print(f"\nWritten to {CACHE_PATH}")


if __name__ == "__main__":
    main()
