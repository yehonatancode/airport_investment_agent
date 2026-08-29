# Airport Investment Agent

An agent that answers questions about U.S. airports — congestion, growth
trends, long-haul mix, and which airports look like strong candidates for
terminal expansion — built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
(`ClaudeSDKClient`, `@tool`, `create_sdk_mcp_server`), backed by real public
BTS aviation data and a deterministic (non-LLM) scoring engine.

## What it does

- Fetches real per-airport stats (operations, delay minutes, passenger
  growth, route/destination growth, long-haul share) from the Bureau of
  Transportation Statistics — no mocked data.
- Scores airports as "strong candidates" via a deterministic gate (operations
  + delay must both clear absolute thresholds, with a soft ramp instead of a
  cliff) times a weighted modifier composite — see [DESIGN.md](DESIGN.md) for
  the full rationale, thresholds, and known limitations.
- Ranks/filters by US Census region (e.g. "strong candidates in New
  England"), always grounded in real tool output.
- Runs as a persistent conversational session, so follow-up questions keep
  context.

## Data sources

- **On-Time Performance** (transtats.bts.gov, monthly) — delay, ops, distance/long-haul.
- **DB1B Market Survey** (transtats.bts.gov, quarterly, 10% ticket sample) — passenger and route growth.
- **T-100 Domestic Market and Segment Data** (BTS ArcGIS FeatureServer) — annual cross-check.

All three are free and require no API key. `load_factor_trend` is the one
locked scoring factor that isn't available — see DESIGN.md for why.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Requires `ANTHROPIC_API_KEY` in the environment (or an active `ant auth login` profile).

## Running it

```bash
python3 cli.py            # interactive chat
python3 cli.py --test     # runs the assignment's example questions + edge cases
```

First-time queries against a new time period download and cache the
underlying BTS files locally (`data_cache/`, gitignored - a few minutes,
multi-GB). `airport_stats_cache.json` in this repo pre-computes scores for
76 major airports (all FAA Large Hub airports plus a set of Medium Hub
airports) so most queries resolve instantly without that wait; anything
outside the cache still resolves correctly, just live.

## Files

| File | Purpose |
|---|---|
| `airport_stats_tool.py` | Fetch tool - pulls and aggregates real BTS data per airport |
| `scoring_tool.py` | Deterministic gate + modifier scoring engine |
| `cli.py` | Persistent chat session (interactive or `--test`) |
| `generate_cache.py` | Builds `airport_stats_cache.json` |
| `DESIGN.md` | Assumptions, tradeoffs, and known limitations |
| `toy_tool.py` | Minimal reference example of the SDK's `@tool` pattern |
