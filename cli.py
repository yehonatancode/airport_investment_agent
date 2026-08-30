"""Phase 2 CLI: a persistent chat session over the fetch + scoring tools.

Uses one ClaudeSDKClient for the whole run, so every client.query() call
after the first is a genuine follow-up with full prior context - no manual
history bookkeeping needed, the SDK keeps the session.

Usage:
    python3 cli.py            # interactive chat loop
    python3 cli.py --test     # runs the 4 assignment example questions + a
                               # zero-candidates follow-up, then exits
"""

import asyncio
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
)

from airport_stats_tool import fetch_airport_stats
from scoring_tool import rank_airports_in_region_tool, score_airport_tool

data_server = create_sdk_mcp_server(name="airport_data", tools=[fetch_airport_stats])
scoring_server = create_sdk_mcp_server(
    name="airport_scoring", tools=[score_airport_tool, rank_airports_in_region_tool]
)

SYSTEM_PROMPT = (
    "You are an airport investment analyst agent. You have tools to fetch real "
    "BTS aviation stats for one airport (fetch_airport_stats), to compute a "
    "deterministic strong-candidate score for one airport (score_airport), and "
    "to rank all airports in a US Census region (rank_airports_in_region). "
    "Always call a tool to get real numbers before answering a question about "
    "specific airports - never guess stats from memory or training data. "
    "When asked to find, rank, or compare candidates in a region, use "
    "rank_airports_in_region rather than guessing which airports are relevant. "
    "Always explain your reasoning by citing the specific numbers that drove "
    "your answer - which gate factors passed or failed, and which modifier "
    "factors moved the score. If a factor such as load_factor_trend is null "
    "or unavailable, say so explicitly and explain how its absence affects "
    "your confidence, rather than ignoring the gap or inventing a number."
)

TEST_QUESTIONS = [
    "Which airports in New England are strong candidates for terminal expansion?",
    "What about the smaller airports in that list - excluding Boston, Hartford, "
    "and Providence, are any of the rest strong candidates too?",
    "How confident are you in Boston's final score, and has it changed "
    "recently - or would you have any way of knowing if it had?",
    "How would you assess Chicago O'Hare (ORD) as a candidate, given its delay numbers?",
    "Compare LA and Santa Ana airport congestion levels.",
    "What is the percentage of long haul flights out of Anchorage airport?",
    "What is the unmet flight demand in SFO airport and why?",
]


def build_agent_options() -> ClaudeAgentOptions:
    """The one place agent wiring (servers, allowed tools, model, prompt) is
    defined. Both the CLI and the Streamlit UI call this - neither forks its
    own copy of the tool-registration/options logic.
    """
    return ClaudeAgentOptions(
        mcp_servers={"airport_data": data_server, "airport_scoring": scoring_server},
        # tools=[] disables the entire built-in Claude Code toolset (Bash,
        # Read, Edit, Task, etc.) - none of which this agent ever needs.
        # allowed_tools alone doesn't do this: it only controls which tools
        # can be auto-invoked without a permission prompt, not which tools
        # are advertised to the model at all. With only 3 tools left to
        # advertise, the model doesn't need a ToolSearch round trip to
        # discover them - measured ~15-20% latency reduction on multi-tool
        # queries (see DESIGN.md).
        tools=[],
        allowed_tools=[
            "mcp__airport_data__fetch_airport_stats",
            "mcp__airport_scoring__score_airport",
            "mcp__airport_scoring__rank_airports_in_region",
        ],
        model="claude-sonnet-5",
        system_prompt=SYSTEM_PROMPT,
    )


async def run_turn(client: ClaudeSDKClient, prompt: str) -> list[dict]:
    """Runs one query/response turn on an already-open client and returns
    structured events instead of printing - the shared core so a UI (CLI
    prints, Streamlit renders) can consume the same underlying agent call
    without re-implementing the query/receive_response loop.
    """
    events = []
    await client.query(prompt)
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    events.append({"type": "text", "content": block.text})
                elif isinstance(block, ToolUseBlock):
                    events.append(
                        {"type": "tool_call", "name": block.name, "input": block.input}
                    )
    return events


async def ask(client: ClaudeSDKClient, prompt: str) -> None:
    print(f"\n{'=' * 80}\nYOU: {prompt}\n{'=' * 80}")
    for event in await run_turn(client, prompt):
        if event["type"] == "text":
            print(event["content"])
        elif event["type"] == "tool_call":
            print(f"[tool call] {event['name']}({event['input']})")


async def main() -> None:
    async with ClaudeSDKClient(options=build_agent_options()) as client:
        if "--test" in sys.argv:
            for q in TEST_QUESTIONS:
                await ask(client, q)
            return

        print("Airport investment chat. Type 'exit' to quit.")
        while True:
            try:
                user_input = input("\nYou: ").strip()
            except EOFError:
                break
            if user_input.lower() in ("exit", "quit"):
                break
            if not user_input:
                continue
            await ask(client, user_input)


if __name__ == "__main__":
    asyncio.run(main())
