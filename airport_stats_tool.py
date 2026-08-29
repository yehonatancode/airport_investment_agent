"""Fetch tool: pulls real per-airport aviation stats from free, no-auth BTS sources.

Sources (see conversation for the research that ruled out the alternatives):
- On-Time Performance PREZIP (transtats.bts.gov) - monthly, flight-level records.
  Gives: avg delay minutes, long-haul share, ops count, destination count, state.
- DB1B Market Survey PREZIP (transtats.bts.gov) - quarterly, 10%-sample ticket data.
  Gives: passenger growth trend, route/destination growth.
- T-100 Domestic Market and Segment Data (BTS ArcGIS FeatureServer) - single-year
  (2024) airport-level totals. Used as a cross-check for passenger/ops counts.

load_factor_trend is NOT available from any free, no-auth source: the BTS table
that has seats (T-100 Segment) is only downloadable through a legacy ASP.NET
form postback (__VIEWSTATE/__EVENTVALIDATION), not a stable URL - disproportionate
scraping effort for this phase. It is returned as null with a reason string.

Both BTS files are parsed once per period into an in-process aggregate keyed by
every origin airport (not just the one asked about), so a region-wide query
(Phase 2) costs one file pass total instead of one pass per airport.
"""

import csv
import json
import urllib.request
import zipfile
from pathlib import Path

from claude_agent_sdk import tool

CACHE_DIR = Path(__file__).parent / "data_cache"
CACHE_DIR.mkdir(exist_ok=True)

ONTIME_URL = (
    "https://transtats.bts.gov/PREZIP/"
    "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
)
DB1B_URL = (
    "https://transtats.bts.gov/PREZIP/"
    "Origin_and_Destination_Survey_DB1BMarket_{year}_{quarter}.zip"
)
T100_QUERY_URL = (
    "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
    "T100_Domestic_Market_and_Segment_Data/FeatureServer/1/query"
)

# Default comparison window: latest full year with clean BTS data vs. the year before.
CURRENT_YEAR, CURRENT_MONTH, CURRENT_QUARTER = 2024, 1, 1
PRIOR_YEAR, PRIOR_MONTH, PRIOR_QUARTER = 2023, 1, 1

LONG_HAUL_MILES = 1500  # domestic long-haul threshold, in nonstop miles

_EMPTY_ONTIME_STATS = {
    "operations": 0,
    "avg_dep_delay_minutes": None,
    "avg_arr_delay_minutes": None,
    "avg_distance_miles": None,
    "long_haul_share_pct": None,
    "distinct_destinations": 0,
    "state": None,
}
_EMPTY_DB1B_STATS = {"estimated_passengers": 0, "distinct_markets": 0}

_ontime_agg_cache: dict[tuple[int, int], dict[str, dict]] = {}
_db1b_agg_cache: dict[tuple[int, int], dict[str, dict]] = {}


def _download_and_extract(url: str, cache_key: str) -> Path:
    """Download a BTS PREZIP file once and cache the extracted CSV locally."""
    zip_path = CACHE_DIR / f"{cache_key}.zip"
    extract_dir = CACHE_DIR / cache_key
    if not extract_dir.exists():
        if not zip_path.exists():
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(zip_path, "wb") as f:
                f.write(resp.read())
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
    csvs = list(extract_dir.glob("*.csv"))
    if not csvs:
        raise RuntimeError(f"No CSV found after extracting {url}")
    return csvs[0]


def _load_ontime_aggregates(year: int, month: int) -> dict[str, dict]:
    """One pass over the monthly On-Time Performance file, grouped by every origin."""
    key = (year, month)
    if key in _ontime_agg_cache:
        return _ontime_agg_cache[key]

    csv_path = _download_and_extract(
        ONTIME_URL.format(year=year, month=month), f"ontime_{year}_{month}"
    )
    raw: dict[str, dict] = {}

    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Cancelled") == "1.00":
                continue
            origin = row.get("Origin")
            if not origin:
                continue
            a = raw.setdefault(
                origin,
                {
                    "ops": 0,
                    "dep_delay_sum": 0.0,
                    "dep_delay_n": 0,
                    "arr_delay_sum": 0.0,
                    "arr_delay_n": 0,
                    "distance_sum": 0.0,
                    "long_haul_count": 0,
                    "destinations": set(),
                    "state": row.get("OriginState"),
                },
            )
            a["ops"] += 1
            a["destinations"].add(row.get("Dest"))

            dep = row.get("DepDelayMinutes")
            if dep not in (None, ""):
                a["dep_delay_sum"] += float(dep)
                a["dep_delay_n"] += 1
            arr = row.get("ArrDelayMinutes")
            if arr not in (None, ""):
                a["arr_delay_sum"] += float(arr)
                a["arr_delay_n"] += 1
            dist = row.get("Distance")
            if dist not in (None, ""):
                distance = float(dist)
                a["distance_sum"] += distance
                if distance >= LONG_HAUL_MILES:
                    a["long_haul_count"] += 1

    result = {}
    for origin, a in raw.items():
        ops = a["ops"]
        result[origin] = {
            "operations": ops,
            "avg_dep_delay_minutes": (
                round(a["dep_delay_sum"] / a["dep_delay_n"], 2) if a["dep_delay_n"] else None
            ),
            "avg_arr_delay_minutes": (
                round(a["arr_delay_sum"] / a["arr_delay_n"], 2) if a["arr_delay_n"] else None
            ),
            "avg_distance_miles": round(a["distance_sum"] / ops, 1) if ops else None,
            "long_haul_share_pct": round(100 * a["long_haul_count"] / ops, 1) if ops else None,
            "distinct_destinations": len(a["destinations"]),
            "state": a["state"],
        }
    _ontime_agg_cache[key] = result
    return result


def _ontime_stats(year: int, month: int, airport: str) -> dict:
    return _load_ontime_aggregates(year, month).get(airport, _EMPTY_ONTIME_STATS)


def _load_db1b_aggregates(year: int, quarter: int) -> dict[str, dict]:
    """One pass over the quarterly DB1B Market file, grouped by every origin."""
    key = (year, quarter)
    if key in _db1b_agg_cache:
        return _db1b_agg_cache[key]

    csv_path = _download_and_extract(
        DB1B_URL.format(year=year, quarter=quarter), f"db1b_{year}_{quarter}"
    )
    raw: dict[str, dict] = {}

    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            origin = row.get("Origin")
            if not origin:
                continue
            a = raw.setdefault(origin, {"passengers": 0.0, "destinations": set()})
            pax = row.get("Passengers")
            if pax not in (None, ""):
                a["passengers"] += float(pax)
            a["destinations"].add(row.get("Dest"))

    # DB1B Market is a 10% sample of tickets; scale up to estimate the true total.
    result = {
        origin: {
            "estimated_passengers": round(a["passengers"] * 10),
            "distinct_markets": len(a["destinations"]),
        }
        for origin, a in raw.items()
    }
    _db1b_agg_cache[key] = result
    return result


def _db1b_stats(year: int, quarter: int, airport: str) -> dict:
    return _load_db1b_aggregates(year, quarter).get(airport, _EMPTY_DB1B_STATS)


def _pct_growth(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return round(100 * (current - prior) / prior, 1)


def _fetch_t100_aggregate(airport: str) -> dict:
    params = f"where=origin='{airport}'&outFields=*&f=json"
    url = f"{T100_QUERY_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    features = data.get("features", [])
    if not features:
        return {"year": None, "passengers": None, "enplanements": None}
    attrs = features[0]["attributes"]
    return {
        "year": attrs.get("year"),
        "passengers": attrs.get("passengers"),
        "enplanements": attrs.get("enplanements"),
    }


def redistribute_weights(
    available_factors: dict[str, float | None], original_weights: dict[str, float]
) -> dict[str, float]:
    """Rescale modifier weights to sum to 100%, dropping factors with no data.

    Runs per-airport at score time (not once globally) so it also covers a
    factor that's missing for one specific airport, not just a source-wide gap
    like load_factor_trend today.
    """
    usable = {k: w for k, w in original_weights.items() if available_factors.get(k) is not None}
    total = sum(usable.values())
    if total == 0:
        return {k: 0.0 for k in original_weights}
    return {k: round(100 * w / total, 4) for k, w in usable.items()}


def airports_in_region(states: set[str], year: int = CURRENT_YEAR, month: int = CURRENT_MONTH) -> list[str]:
    """All airport codes that operated flights from any of the given states this period."""
    aggregates = _load_ontime_aggregates(year, month)
    return [code for code, stats in aggregates.items() if stats.get("state") in states]


def build_airport_stats(airport_code: str) -> dict:
    airport = airport_code.strip().upper()

    current_ontime = _ontime_stats(CURRENT_YEAR, CURRENT_MONTH, airport)
    prior_ontime = _ontime_stats(PRIOR_YEAR, PRIOR_MONTH, airport)
    current_db1b = _db1b_stats(CURRENT_YEAR, CURRENT_QUARTER, airport)
    prior_db1b = _db1b_stats(PRIOR_YEAR, PRIOR_QUARTER, airport)
    t100 = _fetch_t100_aggregate(airport)

    return {
        "airport_code": airport,
        "state": current_ontime.get("state"),
        "period": {
            "ontime_current": f"{CURRENT_YEAR}-{CURRENT_MONTH:02d}",
            "ontime_prior": f"{PRIOR_YEAR}-{PRIOR_MONTH:02d}",
            "db1b_current": f"{CURRENT_YEAR}Q{CURRENT_QUARTER}",
            "db1b_prior": f"{PRIOR_YEAR}Q{PRIOR_QUARTER}",
        },
        "ops": {
            "operations": current_ontime["operations"],
            "operations_prior_year": prior_ontime["operations"],
            "operations_growth_pct": _pct_growth(
                current_ontime["operations"], prior_ontime["operations"]
            ),
        },
        "avg_delay_minutes": {
            "departure": current_ontime["avg_dep_delay_minutes"],
            "arrival": current_ontime["avg_arr_delay_minutes"],
        },
        "passenger_growth_trend": {
            "current_estimated_passengers": current_db1b["estimated_passengers"],
            "prior_estimated_passengers": prior_db1b["estimated_passengers"],
            "growth_pct": _pct_growth(
                current_db1b["estimated_passengers"], prior_db1b["estimated_passengers"]
            ),
            "source": "DB1B Market Survey, 10% sample scaled x10",
        },
        "route_destination_growth": {
            "current_distinct_destinations": current_db1b["distinct_markets"],
            "prior_distinct_destinations": prior_db1b["distinct_markets"],
            "growth_pct": _pct_growth(
                current_db1b["distinct_markets"], prior_db1b["distinct_markets"]
            ),
        },
        "long_haul_share": {
            "share_pct": current_ontime["long_haul_share_pct"],
            "threshold_miles": LONG_HAUL_MILES,
        },
        "load_factor_trend": {
            "value": None,
            "status": "unavailable_this_phase",
            "reason": (
                "T-100 Segment (the table with seats/capacity) is only downloadable "
                "via a legacy ASP.NET form postback, not a stable URL - out of scope "
                "for this phase."
            ),
        },
        "t100_cross_check": t100,
    }


@tool(
    "fetch_airport_stats",
    "Fetch real per-airport aviation stats (ops, delay, passenger/route growth, "
    "long-haul share) from free BTS sources, keyed by IATA airport code.",
    {"airport_code": str},
)
async def fetch_airport_stats(args):
    result = build_airport_stats(args["airport_code"])
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
