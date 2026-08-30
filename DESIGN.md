# Design Document — Airport Investment Intelligence Agent

## Development Process

Before diving into a topic this wide, I like to research best practices and get my hands dirty with the underlying logic rather than jump straight to code. This build was done as active pair-programming with Claude — not delegated end to end — with explicit checkpoints after every phase where I had to understand and be able to defend the mechanism before continuing. That mattered for two reasons: I need to be able to stand behind every line of this code, and catching an architectural issue early is far cheaper than catching it once the system has grown complex around it. Three real bugs were caught this way, not through code review alone.

## 1. Overview

This agent answers investment-oriented questions about U.S. airports — which airports show real congestion/demand strain, how they compare on delay and traffic mix, and which look like strong candidates for terminal expansion — by combining real public BTS aviation data with a fully deterministic scoring engine, wrapped in a Claude Agent SDK tool interface (`ClaudeSDKClient`, `@tool`, `create_sdk_mcp_server`) so an LLM can fetch, reason about, and narrate the results conversationally, including follow-up questions, without ever performing the underlying arithmetic itself.

## 2. Scoring Methodology

After digging into what actually makes a good renovation option, weighing the pros and cons of each candidate factor together, congestion kept coming out on top. It's the most direct evidence that an airport is running out of room. Delay rate came right behind it, mostly as another indicator for the complex capacity numbers we couldn't easily get otherwise. Everything else — growth, new routes, long-haul mix — matters, but only as evidence of *how much* the investment case matters, not whether one exists in the first place.

That's why we didn't stop at weighting congestion heavily — we made it, together with delay, a gate instead. <u>A weighted score can quietly let a strong number in one place cover for a weak one somewhere else — it never really says "this doesn't apply here."</u> But an airport with great growth and zero congestion isn't actually the airport we're looking for — there's nothing to relieve. Making congestion + delay a gate keeps that logic honest: growth and route expansion only count once there's real evidence something needs fixing.

**Gate**
- Inputs: operations (ops/capacity proxy) + avg departure delay
- Combination: `min()` of the two per-factor multipliers — both must hold
- Not a step function: ±10% ramp band around each threshold, linear 0→1
- Thresholds are real-data-grounded (Jan 2024 national percentiles: ops ~P75, delay ~median) — tunable, not final
- `near_threshold` flag set whenever either raw value falls inside its ramp band

**Modifiers**
- Weights: passenger growth 40%, route/destination growth 30%, load factor 20%, long-haul share 10%
- `load_factor_trend` is currently always null → weights redistributed proportionally among the other three (currently 50/37.5/12.5) via `redistribute_weights()`, per-airport, at score time
- Each factor normalized onto a fixed 0–100 scale (hardcoded absolute bounds, clamped at the edges) **before** weighting — not query-relative, so the same airport means the same thing everywhere it's compared

`final_score = gate_combined_multiplier × modifier_composite_0_100`

## 3. Known Bugs Found & Fixed

We caught three real bugs along the way through actual testing, not just reading the code and assuming it was right. Each one surfaced as a number that didn't add up, and got resolved by walking through the math by hand until the real explanation showed itself.

**Bug 1 — Modifier scale mismatch.** Weights were applied to raw, differently-scaled values (a 0–100 share vs. single-digit growth percentages), so the weighting didn't actually control influence the way it was supposed to. Caught by hand-verifying BOS's numbers: long-haul share (12.5% weight) was outcontributing passenger growth (50% weight). Fixed by normalizing every factor onto a common scale before weighting, not after.

**Bug 2 — Delay gate direction inverted.** Delay was originally scored "lower is better," the opposite of the actual investment thesis — elevated delay is supposed to be a congestion signal, not a quality defect. Caught because ORD, a genuinely congested major hub, scored a flat 0.0 — identical to an airport with almost no traffic at all, for two completely different and non-comparable reasons. Fixed by flipping the direction and renaming the threshold variable so it can't mislead a future reader the same way again.

**Bug 3 — Region tool grounding gap.** The region-ranking tool only returned airports that passed the gate, so a natural follow-up ("what about the smaller airports") forced the agent to guess airport codes from its own training knowledge — including one guess that wasn't even a real airport in this dataset. Fixed by always returning every airport evaluated, cleared or not.

## 4. Known Limitations & Assumptions

- **`load_factor_trend` is null for every airport, always.** I didn't want to spend important development time chasing one sub-factor buried behind a legacy form-based download with no stable URL — five of six factors were fully available from confirmed, reliable sources, and closing this gap felt like the wrong place to spend limited hours. `redistribute_weights()` handles this generically (it rescales whatever weights remain, for any missing factor — not a load-factor special case), currently producing 50/37.5/12.5 across the other three modifiers.
- **Passenger and route growth rely on a 10% sample.** These figures come from BTS's DB1B Market Survey, a 10%-sample of ticketed itineraries scaled ×10 to estimate real totals, updated quarterly rather than continuously — a real, acknowledged limitation of the chosen source, separate from the `load_factor` gap.
- **No automatic formula-staleness detection.** `FORMULA_VERSION` lets a score be checked against the current arithmetic, but nothing checks it automatically. Tested directly: the agent correctly reasoned about *data* staleness on its own (computed the ~2.5-year gap between the data period and today unprompted), but has no way to independently detect *methodology* staleness without the version field being explicitly compared. `build_airport_stats()`'s cache-first lookup now checks this field itself before trusting a cached entry - a stale `formula_version` is treated as a cache miss and falls through to a live fetch, which is what makes the field functionally load-bearing rather than just informational.
- **~55% of the 76 cached airports score exactly 0.0.** Verified, intended consequence of the corrected gate direction — many large and medium hubs run efficiently (low average delay), show no congestion signal under this design, and are correctly excluded as "strong candidates" by that definition.
- **Anchorage (ANC) hub-classification correction.** Initially assumed Large Hub; real 2024 BTS enplanement share (0.316%) places it as Medium Hub. Included in the committed cache via the "already manually verified" carveout, not because it independently qualifies.
- **Data currency.** All stats are anchored to a single fixed snapshot — January 2024 (On-Time Performance) and Q1 2024 (DB1B) — compared against the same period one year prior. Not a full year, not seasonally adjusted, and not current as of whenever this is read.
- **Piper voices are en-US only, no regional variety.** Bot's robotic character comes from using a genuinely lower-fidelity voice tier (`en_US-danny-low`) rather than a pitch adjustment on a good voice, since Piper's `SynthesisConfig` doesn't expose native pitch control the way browser TTS did. Piper's native espeak-ng dependency can also fail to initialize on some fresh installs (a known packaging quirk, not introduced by this project) - confirmed on a genuinely fresh clone, where it crashed the whole process the first time. Voice synthesis now runs in an isolated subprocess specifically so that failure mode degrades gracefully (the text response still renders, a small "voice unavailable" note appears) instead of taking the app down, at the cost of a ~1-2s per-response overhead for spawning that process. The "Speak responses aloud" toggle defaults to off for the same reason - voice is opt-in on a fresh install rather than something a reviewer can hit by surprise. Separately, Piper synthesis occasionally times out inside its isolated subprocess - observed across all three presets in testing (Female, Male, and Bot each failed at least once and succeeded at least once across repeated trials), not specific to one voice, and the root cause hasn't been identified. The default preset was changed to Male on the assumption it was more reliable, but further testing showed that assumption doesn't hold - the timeout is intermittent regardless of preset. When it happens, the existing timeout/crash isolation already handles it correctly: the text response completes normally and a "Voice unavailable" note appears instead of audio. Treated as a known, accepted limitation of a bonus feature, not a core product risk - not worth further investigation time this close to submission.
- **Piper synthesis occasionally times out.** In some runs, voice generation hangs in its isolated subprocess and hits a timeout ceiling — observed across all three presets (Female, Male, Bot), not specific to one voice. Root cause not identified; spawn overhead and Gatekeeper quarantine flags were checked and ruled out as likely causes, but the investigation was intentionally stopped short of a full fix given submission timing. The existing crash/timeout isolation already handles this correctly — the text response completes and renders normally, a "Voice unavailable" message shows in place of audio, and the app never breaks. This is an accepted limitation of a bonus feature, not a risk to the core product.
- **Repeated or rephrased identical questions re-run the full agent pipeline, not cached.** The existing `airport_stats_cache.json` avoids redundant *data* fetches, but the LLM's response generation itself isn't cached. A naive text-match cache was considered and deliberately not built, since it risks returning a stale or context-blind answer in a conversational agent where the same question can mean different things depending on prior turns.

## 5. Where AI Is Used vs. Deterministic Logic

First and foremost, the assignment had a clear requirement to make this deterministic. Beyond that: every AI model tells you upfront that it can make mistakes and shouldn't be followed blindly. When the subject is investment decisions, that warning isn't hypothetical — it's the difference between a bug and a liability. Imagine the agent making up a number that sounds real but isn't, and an investor acting on it — that's not a minor UX issue, that's the kind of mistake that ends in a lawsuit. If we're building a tool people are actually meant to rely on, it simply cannot afford the option of hallucinations. The engineer must understand the business and the business model, and be able to develop the right solutions accordingly.

- **Scoring arithmetic** (gate multipliers, normalization, weighting, `final_score`): 100% deterministic Python, zero LLM involvement.
- **LLM (Claude, via the Agent SDK) role**: tool orchestration (deciding which tool/args to call), narration of results (explaining which sub-scores drove an answer), conversational follow-up handling.
- **Concrete example of intended behavior**: the SFO "unmet flight demand" question — no tool computes that metric, and the agent's tested behavior was to say so explicitly and refuse to fabricate a number, rather than inventing one.

## 6. Key Tradeoffs

### Data source choice
The real decision wasn't which BTS files to use — those were confirmed free and live quickly. It was whether to spend meaningful development time chasing one sub-factor (`load_factor`) buried behind a legacy form-based download with no stable URL. Given the one-day time budget, that felt like the wrong place to spend hours — I didn't want to waste important development time on a sub-factor I could reasonably add back in later. I shipped with the five real, reliable factors and documented the gap plainly rather than let it become a blocker.

### Cache tiering
We started conservative: commit to Large hub airports only, treat Medium hubs as a stretch goal, and only chase them if time allowed — the goal was protecting the guaranteed deliverable from an unknown cost. Once real numbers came in (41 airports in about 90 seconds), the concern that shaped that caution no longer applied, so we ran both tiers in the same sitting. The lesson isn't "we got lucky" — it's to scope conservatively when a cost is unknown, then re-check the real numbers before locking a decision in.

### Framework choice
Now that I've actually built with it, Claude Agent SDK held up as a solid choice — not just on paper, but in practice. What stood out most was how transparent the internal agent logic is: watching the message stream as tool-use blocks and text blocks, seeing exactly when and why a tool gets called, made the "agent" concept click in a way reading about frameworks abstractly never would have. The `@tool`/`create_sdk_mcp_server` pattern mapped directly onto what this assignment actually needed — a way to keep the scoring math completely deterministic and separate from anything the LLM touches, while still letting Claude decide when and how to use it. That's not just "easy to learn" — the framework's basic building blocks happened to line up with the exact requirement that mattered most here. Worth noting honestly: the SDK is built around Claude Code's own coding-agent shape by default (file editing, bash); I used only its custom-tool and conversation-loop pieces, a legitimate but narrower slice of what the framework can do.

### Multi-tool-call latency
Multi-airport questions take longer due to sequential tool calls and an SDK-level tool-selection step — a two-airport comparison currently takes around 80 seconds. Batching or parallelizing these calls is a natural next optimization, deliberately not attempted here given the time budget. This is a known, honest tradeoff, not an oversight.

## 7. Testing & Verification Approach

Most of the confidence in this build didn't come from things looking right on the surface — it came from deliberately checking numbers by hand at every stage: recomputing a real airport's score manually against the code's own output, stress-testing boundary cases (a growth value outside the normal range, a delay figure sitting right on a threshold), and re-running the full question set after every fix instead of assuming a fix worked. Three real bugs were caught this way.

## 8. Interface & Bonus Features

### Accessibility as a deliberate goal, not a bonus checkbox
I don't think a product is really finished if only programmers can use it comfortably. A CLI works fine for me, but it's a real barrier for a non-technical stakeholder — and given this tool is meant to support investment analysts, not developers, that's a form of unfairness I didn't want to build in by default. The moment I saw the working Streamlit interface, I knew that was the right direction — low-friction, usable by anyone, no terminal required.

### Voice output, and why voice input was deliberately not built
Voice was something I wanted for the same reason, and honestly because I find the technology genuinely exciting. But once I understood what real voice *input* would require given the interface I'd already built — Streamlit's execution model has no clean, lightweight way to get a live browser transcript back into a running Python session without either a full custom frontend build or a workaround that risks tearing down the persistent chat session — I made a deliberate call not to chase it. Rebuilding the architecture around it would have meant I no longer fully understood what was running underneath, and that's a line I wasn't willing to cross, no matter how impressive voice input would have been. Standing behind every part of this code mattered more than checking every possible box.

Voice output didn't carry that risk — it's one-way, nothing talks back into the session. Getting it to sound decent took some real back-and-forth.

Browser voices turned out too flat and robotic, so I switched to Piper — a free, local, open-source neural TTS engine. Male and Female use two genuinely different, natural-sounding models, confirmed as truly distinct audio, not just different labels. Bot uses a lower-quality model on purpose, leaning into the robotic sound rather than faking it with a pitch trick.

Testing this live surfaced two small but useful additions: a replay button that re-speaks an answer without triggering a new LLM turn, and a loading indicator during synthesis, since Piper isn't instant and a silent gap felt like something had broken.

## 9. Setup / Run Instructions

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Requires `ANTHROPIC_API_KEY` in the environment.

```bash
python3 cli.py            # interactive chat
python3 cli.py --test     # runs the assignment's example questions + edge cases
streamlit run streamlit_app.py   # web UI with voice output
```

A first-time query against a new time period downloads and caches the underlying BTS files locally (`data_cache/`, gitignored, multi-GB). The committed `airport_stats_cache.json` pre-computes scores for 76 major airports so most queries resolve instantly without that wait; anything outside the cache still resolves correctly, just live.