"""Deterministic scoring engine for airport "strong candidate" ranking.

No LLM in the arithmetic anywhere in this file - the agent (Sonnet) narrates
these numbers, it never computes them. This is the exact locked design from
the conversation:

GATE (both factors must individually clear their own threshold):
  - ops (a proxy for "ops/capacity ratio" - see caveat below)
  - avg departure delay minutes
Both factors work the same direction: HIGH values pass the gate. Ops
measures scale; delay is a symptom of real congestion, which is the thing
this score is meant to surface (an airport under strain is the investment
case, not a smoothly-running one) - so elevated delay passes, low delay
fails, same as ops. The gate is not a step function: each factor gets a
+/-10% ramp zone around its threshold where the multiplier moves linearly
from 0 to 1 instead of cliff-dropping. The combined gate multiplier is the
weaker of the two (both must hold). Airports whose raw value lands inside
either ramp zone are flagged near_threshold.

CAVEAT (ops/capacity ratio): a true capacity denominator (seats, runway/gate
throughput) is not available from any free source - the same T-100 Segment
wall that blocks load_factor_trend (see airport_stats_tool.py). The gate's
"ops" side is therefore raw monthly operations count, used as a scale proxy:
"is this airport busy enough to matter." Flagged as a placeholder pending
real capacity data, not treated as final.

THRESHOLD MODE: thresholds are always absolute, computed once against the
full national distribution - never recomputed against a filtered subset.
A region/percentile query (e.g. "top 20% in New England") is a VIEW: filter
to the region, keep only airports that already cleared the absolute gate,
then rank/slice by percentile within that subset. If nothing in the subset
clears the gate, the correct answer is "no strong candidates here right
now" - never the least-bad airport dressed up as a good one.

MODIFIERS (weighted on top of the gate multiplier, applied only once gated):
  passenger growth trend 40%, route/destination growth 30%,
  load factor trend 20%, long-haul share 10%.
load_factor_trend is null for every airport at this data-source stage, so
redistribute_weights() (from airport_stats_tool.py, written in Phase 1)
rescales the remaining three to 50/37.5/12.5 automatically, per-airport.
"""

import json
import math

from claude_agent_sdk import tool

from airport_stats_tool import (
    CURRENT_MONTH,
    CURRENT_YEAR,
    airports_in_region,
    build_airport_stats,
    redistribute_weights,
)

# Data-grounded starting thresholds: computed from the actual national
# distribution of the 334 US origin airports in the Jan 2024 On-Time
# Performance file (see conversation for the percentile computation).
# min_operations ~ national P75 (top-quartile busy-ness).
# min_avg_delay_minutes ~ national median - a floor, not a ceiling: delay
# AT OR ABOVE this is the "elevated/congested" signal that passes the gate.
# Both are tunable assumptions, not final numbers - revisit with a full
# year (seasonality) and a real capacity denominator once available.
GATE_THRESHOLDS = {
    "min_operations": 1000,
    "min_avg_delay_minutes": 20.0,
}
RAMP_PCT = 0.10  # +/-10% ramp zone around each threshold, not a step function

# Bump this string any time compute_gate / compute_modifier_composite /
# score_airport's arithmetic changes. Stamped on every score so a cached
# entry (or a fresh call) can be checked against the current formula
# without re-deriving history - the agent has no other way to tell a
# stale-methodology score from a stale-data one (see conversation).
# v1: original - unnormalized modifiers, delay direction inverted (low delay passed).
# v2: modifiers normalized onto 0-100 with fixed bounds before weighting (Fix 1).
# v3: delay direction corrected - elevated delay (>= threshold) now passes the gate.
FORMULA_VERSION = "v3"

MODIFIER_WEIGHTS = {
    "passenger_growth_trend": 40.0,
    "route_destination_growth": 30.0,
    "load_factor_trend": 20.0,
    "long_haul_share": 10.0,
}

# Fixed, hardcoded absolute bounds each modifier is normalized onto a common
# 0-100 scale with, BEFORE weights are applied. Without this, a raw share
# value (long_haul_share, naturally 0-100) and a raw growth rate (typically
# single digits, occasionally 20-50% off a tiny base) get summed directly -
# the weight percentages then don't actually control influence, since the
# inputs aren't on comparable scales. Bounds below are starting assumptions
# from the real ranges seen in Phase 1/2 testing (growth ranged roughly -18%
# to +49% across the airports tested, most commonly single digits); revisit
# with a full year of data. Fixed and query-independent, per the same
# "one canonical score" principle already locked for the gate thresholds.
MODIFIER_BOUNDS = {
    "passenger_growth_trend": (-15.0, 20.0),
    "route_destination_growth": (-15.0, 20.0),
    "load_factor_trend": (-15.0, 20.0),  # not yet populated; same growth-rate kind
    "long_haul_share": (0.0, 100.0),  # already a natural 0-100 share
}

# US Census Bureau regional divisions, by 2-letter state code (as reported
# in BTS's OriginState field). Only the divisions needed for now.
REGION_STATES = {
    "new england": {"CT", "ME", "MA", "NH", "RI", "VT"},
    "mid-atlantic": {"NY", "NJ", "PA"},
    "south atlantic": {"DE", "MD", "DC", "VA", "WV", "NC", "SC", "GA", "FL"},
    "east north central": {"OH", "IN", "IL", "MI", "WI"},
    "west north central": {"MN", "IA", "MO", "ND", "SD", "NE", "KS"},
    "east south central": {"KY", "TN", "MS", "AL"},
    "west south central": {"AR", "LA", "OK", "TX"},
    "mountain": {"MT", "ID", "WY", "CO", "NM", "AZ", "UT", "NV"},
    "pacific": {"WA", "OR", "CA", "AK", "HI"},
}


def _ramp_multiplier(value: float, threshold: float, higher_is_better: bool) -> tuple[float, bool]:
    """Linear ramp in [0, 1] across a +/-RAMP_PCT band around threshold.

    Returns (multiplier, near_threshold). near_threshold is True whenever the
    raw value falls inside the ramp band, regardless of which side it's on.
    """
    band = threshold * RAMP_PCT
    lo, hi = threshold - band, threshold + band
    near = lo <= value <= hi

    if higher_is_better:
        if value >= hi:
            return 1.0, near
        if value <= lo:
            return 0.0, near
        return (value - lo) / (hi - lo), near
    else:
        if value <= lo:
            return 1.0, near
        if value >= hi:
            return 0.0, near
        return (hi - value) / (hi - lo), near


def compute_gate(operations: float, avg_departure_delay: float) -> dict:
    ops_mult, ops_near = _ramp_multiplier(
        operations, GATE_THRESHOLDS["min_operations"], higher_is_better=True
    )
    delay_mult, delay_near = _ramp_multiplier(
        avg_departure_delay, GATE_THRESHOLDS["min_avg_delay_minutes"], higher_is_better=True
    )
    combined = min(ops_mult, delay_mult)  # both must hold - weakest link wins
    return {
        "ops_multiplier": round(ops_mult, 3),
        "delay_multiplier": round(delay_mult, 3),
        "combined_multiplier": round(combined, 3),
        "near_threshold": ops_near or delay_near,
        "cleared": combined > 0.0,
        "inputs": {"operations": operations, "avg_departure_delay_minutes": avg_departure_delay},
        "thresholds": GATE_THRESHOLDS,
        "ramp_pct": RAMP_PCT,
    }


def _normalize(value: float, bounds: tuple[float, float]) -> float:
    lo, hi = bounds
    pct = (value - lo) / (hi - lo) * 100
    return round(max(0.0, min(100.0, pct)), 2)


def compute_modifier_composite(factors: dict[str, float | None]) -> dict:
    weights = redistribute_weights(factors, MODIFIER_WEIGHTS)
    normalized = {k: _normalize(factors[k], MODIFIER_BOUNDS[k]) for k in weights}
    composite = sum(normalized[k] * (weights[k] / 100) for k in weights)
    return {
        "raw_factors": factors,
        "normalized_factors_0_100": normalized,
        "bounds_used": {k: MODIFIER_BOUNDS[k] for k in weights},
        "weights_used_pct": weights,
        "composite_0_100": round(composite, 2),
    }


def score_airport(airport_code: str) -> dict:
    stats = build_airport_stats(airport_code)

    gate = compute_gate(
        stats["ops"]["operations"], stats["avg_delay_minutes"]["departure"]
    )

    factors = {
        "passenger_growth_trend": stats["passenger_growth_trend"]["growth_pct"],
        "route_destination_growth": stats["route_destination_growth"]["growth_pct"],
        "load_factor_trend": stats["load_factor_trend"]["value"],
        "long_haul_share": stats["long_haul_share"]["share_pct"],
    }
    modifiers = compute_modifier_composite(factors)

    # composite_0_100 is already a 0-100 quality score (weights applied after
    # normalization, so 50/37.5/12.5 now controls actual influence). The gate
    # multiplier scales it down for a soft/ramp-zone pass and zeroes it
    # entirely for a real fail - it never adds an independent bonus on top.
    final_score = round(gate["combined_multiplier"] * modifiers["composite_0_100"], 1)

    return {
        "airport_code": stats["airport_code"],
        "formula_version": FORMULA_VERSION,
        "state": stats["state"],
        "passed_gate": gate["cleared"],
        "near_threshold": gate["near_threshold"],
        "gate": gate,
        "modifiers": modifiers,
        "final_score": final_score,
        "raw_stats": stats,
    }


def rank_airports(airport_codes: list[str], top_pct: float | None = None) -> dict:
    # Every airport in the region is always scored and returned in
    # all_airports (each with passed_gate/near_threshold) so a follow-up
    # question never has to guess codes from training knowledge - it can
    # always ground on this tool's own output.
    scored = [score_airport(code) for code in airport_codes]
    cleared = sorted(
        (s for s in scored if s["passed_gate"]), key=lambda s: s["final_score"], reverse=True
    )

    if not cleared:
        closest = max(scored, key=lambda s: s["gate"]["combined_multiplier"]) if scored else None
        return {
            "evaluated_count": len(scored),
            "cleared_count": 0,
            "result": "no_strong_candidates",
            "message": (
                f"No airports among the {len(scored)} evaluated cleared the gate "
                "(ops and delay thresholds) right now."
                + (f" Closest was {closest['airport_code']}." if closest else "")
            ),
            "all_airports": scored,
        }

    selected = cleared
    if top_pct is not None:
        k = max(1, math.ceil(len(cleared) * top_pct / 100))
        selected = cleared[:k]

    return {
        "evaluated_count": len(scored),
        "cleared_count": len(cleared),
        "not_cleared_count": len(scored) - len(cleared),
        "result": "ranked",
        "selected": selected,
        "all_airports": scored,
    }


@tool(
    "score_airport",
    "Compute a deterministic strong-candidate score for one airport: a gate "
    "(ops and avg delay must both clear absolute thresholds, with a soft ramp "
    "zone) times a weighted modifier composite (passenger growth, route "
    "growth, load factor, long-haul share). Every sub-score is returned so "
    "you can explain which factors drove the result.",
    {"airport_code": str},
)
async def score_airport_tool(args):
    result = score_airport(args["airport_code"])
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


@tool(
    "rank_airports_in_region",
    "Rank airports in a US Census region by strong-candidate score. Gates "
    "each airport against the fixed national thresholds first, then ranks "
    "only the ones that cleared - optionally sliced to the top N percent of "
    "that cleared subset. If nothing in the region clears the gate, returns "
    "an explicit no_strong_candidates result rather than a fake ranking. "
    "The result always includes all_airports: every airport evaluated in the "
    "region with its passed_gate and near_threshold flags, cleared or not - "
    "use that list for any follow-up about the airports that didn't clear "
    "instead of guessing codes from memory. "
    "Valid regions: " + ", ".join(sorted(REGION_STATES)),
    {"region": str, "top_pct": float},
)
async def rank_airports_in_region_tool(args):
    region = args["region"].strip().lower()
    states = REGION_STATES.get(region)
    if states is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"error": f"Unknown region '{args['region']}'.", "valid_regions": sorted(REGION_STATES)}
                    ),
                }
            ],
            "is_error": True,
        }

    top_pct = args.get("top_pct")
    codes = airports_in_region(states, CURRENT_YEAR, CURRENT_MONTH)
    result = rank_airports(codes, top_pct=top_pct)
    result["region"] = args["region"]
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]}
