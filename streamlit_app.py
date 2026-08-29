"""Web chat UI for the airport investment agent.

Deliberately thin: all agent wiring (MCP servers, allowed tools, model,
system prompt) lives in cli.py's build_agent_options(), and the actual
query/response cycle lives in cli.py's run_turn(). This file only adds a
browser chat rendering on top - it does not define its own ClaudeSDKClient
options or its own query loop, so the CLI and the web UI are guaranteed to
run the exact same agent, not two forks of it.

One ClaudeSDKClient is created once per browser session (st.session_state)
and reused for every message in that session - not recreated per message -
so follow-up questions keep full conversation context, same as the CLI.

Run with: streamlit run streamlit_app.py
"""

import asyncio

import streamlit as st
from claude_agent_sdk import ClaudeSDKClient

from cli import build_agent_options, run_turn

st.set_page_config(page_title="Airport Investment Agent", page_icon="\U0001f6eb")
st.title("Airport Investment Agent")
st.caption(
    "Ask about airport congestion, growth trends, or which airports look like "
    "strong candidates for terminal expansion. Backed by real BTS data and a "
    "deterministic scoring engine - see DESIGN.md for methodology."
)


def get_event_loop() -> asyncio.AbstractEventLoop:
    if "loop" not in st.session_state:
        st.session_state.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(st.session_state.loop)
    return st.session_state.loop


def get_client() -> ClaudeSDKClient:
    if "client" not in st.session_state:
        client = ClaudeSDKClient(options=build_agent_options())
        get_event_loop().run_until_complete(client.__aenter__())
        st.session_state.client = client
    return st.session_state.client


if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

prompt = st.chat_input("Ask about an airport...")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Working..."):
            events = get_event_loop().run_until_complete(run_turn(get_client(), prompt))

        reply_text_parts = []
        for event in events:
            if event["type"] == "tool_call":
                st.caption(f"\U0001f527 called `{event['name']}`  `{event['input']}`")
            elif event["type"] == "text":
                st.markdown(event["content"])
                reply_text_parts.append(event["content"])

    st.session_state.messages.append(
        {"role": "assistant", "content": "\n\n".join(reply_text_parts)}
    )
