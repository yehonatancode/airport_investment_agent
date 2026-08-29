# Design Notes: Assumptions, Tradeoffs & Known Limitations

This file tracks the modeling decisions and data gaps discovered while building
this agent — the "why," not the "what" (the code is the source of truth for
that). Update it whenever a new assumption gets made or an old one gets
revisited.

## Data gaps

- **No real capacity/seat data anywhere.** The BTS table that has it (T-100
  Segment) is only downloadable via a legacy ASP.NET form postback
  (`__VIEWSTATE`/`__EVENTVALIDATION`), not a stable URL — out of scope for
  this build. Two consequences:
  - `load_factor_trend` is null for every airport, always. `redistribute_weights()`
    rescales the other three modifiers (40/30/10 → 50/37.5/12.5) to compensate,
    per-airport, at score time.
  - The gate's "ops/capacity ratio" factor is really just raw operations count
    — a scale proxy, not a true ratio. Flagged as a placeholder, not final.
- **"Unmet flight demand" (the SFO example question) is not a metric any tool
  computes.** There's no bookings-denied, waitlist, or capacity-utilization
  data available. The agent is expected to say so explicitly and offer the
  passenger-growth-vs-ops-decline divergence as an honest, caveated proxy —
  confirmed working correctly in testing, not something to "fix" by adding a
  fake metric.
- **Data is a single fixed snapshot** (Jan 2024 on-time performance, 2024 Q1
  DB1B), compared against the same period one year prior. Not a full year,
  not seasonally adjusted, not current as of today. The agent can detect this
  itself (it correctly computed the ~2.5-year gap between the data period and
  today's date when asked) but **cannot tell if the scoring formula itself is
  stale** — there's no way to distinguish "old data" from "old methodology"
  without the `formula_version` field being checked explicitly. That field
  exists for exactly this reason; bump it on every formula change.

## Gate design

- Thresholds (`min_operations=1000`, `min_avg_delay_minutes=20.0`) are
  data-grounded — computed from real percentiles across the 334 US origin
  airports in the Jan 2024 file (~P75 for ops, ~median for delay) — but are
  single-month numbers, not validated against a full year or multiple years.
  Tunable starting assumptions, not final.
- **Delay direction is intentionally inverted from naive intuition**: elevated
  delay *passes* the gate, low delay *fails* it. The design thesis is that
  delay is a symptom of real congestion/demand strain, which is the
  investment case this score is meant to surface — a smoothly-running airport
  isn't the target. This was implemented backwards once (Fix 2) and is now
  verified correct and consistently narrated by the agent; don't reintroduce
  the "lower delay is better" version.
- **Consequence worth knowing, not a bug**: across the 76-airport cache, 42
  (55%) score exactly 0.0. Most large/medium hubs run efficiently (low
  delay), which zeroes them out by design. A mostly-zero cache is expected
  behavior here, not evidence of a broken pipeline.
- The gate is a soft ramp (±10% band), not a step function, to avoid
  cliff-edge exclusions for airports right at the boundary. Airports inside
  the band are flagged `near_threshold`.

## Modifier design

- Each modifier is normalized onto a fixed, hardcoded 0–100 scale *before*
  weighting (`MODIFIER_BOUNDS`), clamped at the edges — verified correct on
  real out-of-bound data (ORH's +48.8% passenger growth clamps to 100).
  Bounds are absolute and query-independent (never recomputed against a
  filtered subset), matching the same "one canonical score" principle as the
  gate thresholds. Growth-rate bounds (`-15%` to `+20%`) are starting
  assumptions from the ranges actually observed this session (roughly −18%
  to +49%); long-haul share uses its natural 0–100 range.
- Threshold/percentile queries (e.g. "top 20% in a region") are always a
  *view* on top of the already-gated, already-scored set — filtered, then
  ranked. The gate is never recomputed relative to a subset. If nothing in a
  filtered subset clears the gate, the tool returns an explicit
  `no_strong_candidates` result, verified working on a real small-airport
  subset — never a least-bad airport dressed up as a good one.

## Tooling / grounding

- `rank_airports_in_region` always returns **every** airport evaluated in the
  region (`all_airports`, each with `passed_gate`/`near_threshold`), not just
  the ones that cleared. Earlier it only returned cleared airports, which
  forced a follow-up question to guess airport codes from the model's own
  training knowledge rather than tool output — a real grounding gap, now
  fixed and verified (a follow-up that previously needed 7 guessed tool
  calls needed zero after the fix).

## Cache

- `airport_stats_cache.json` covers 76 airports: all 31 FAA-equivalent Large
  Hub airports (≥1.0% of national enplanements, derived directly from BTS
  T-100 domestic data since FAA's own site blocked programmatic access) plus
  10 manually-verified airports that don't independently qualify (e.g.
  Anchorage is actually Medium Hub, not Large Hub, by this measure — despite
  being one of the assignment's example airports), plus 35 additional Medium
  Hub airports.
- **Declining to cache beyond this is a latency tradeoff, not a coverage
  gap.** `fetch_airport_stats` / `score_airport` compute directly from source
  on any cache miss — a reviewer asking about an airport outside the cache
  still gets a correct answer, just not an instant one.
