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
import logging
import re
from pathlib import Path

import streamlit as st
from claude_agent_sdk import ClaudeSDKClient

import piper_worker
from cli import build_agent_options, run_turn

st.set_page_config(page_title="Airport Investment Agent", page_icon="\U0001f6eb")
st.title("Airport Investment Agent")
st.caption(
    "Ask about airport congestion, growth trends, or which airports look like "
    "strong candidates for terminal expansion. Backed by real BTS data and a "
    "deterministic scoring engine - see DESIGN.md for methodology."
)

# Generation is server-side now (Piper - local, offline, neural TTS; no
# browser API, no paid/API-based service). Each preset maps to a real,
# distinct downloaded voice model instead of a name-matching heuristic
# against whatever happens to be installed on the visitor's OS/browser:
# Female/Male use en_US-hfc_female-medium / en_US-hfc_male-medium (chosen
# from Piper's public catalog specifically because their names declare
# gender - not inferred from a name like "Amy" or "Ryan"). Bot uses
# en_US-danny-low - a genuinely lower-quality-tier model (not the same
# model with pitch/rate hacked), which is reliably more robotic/artifact-y
# on its own; rate/pitch are still nudged further for a clearer effect.
# Run `python3 -m piper.download_voices --download-dir piper_voices
# en_US-hfc_female-medium en_US-hfc_male-medium en_US-danny-low` once
# before first use (see README) - models are gitignored, same as
# data_cache/, not committed to the repo.
VOICE_DIR = Path(__file__).parent / "piper_voices"
VOICE_MODELS = {
    "Female": VOICE_DIR / "en_US-hfc_female-medium.onnx",
    "Male": VOICE_DIR / "en_US-hfc_male-medium.onnx",
    "Bot": VOICE_DIR / "en_US-danny-low.onnx",
}


@st.cache_resource(show_spinner="\U0001f50a Starting voice engine...")
def get_piper_worker():
    """Starts the persistent Piper worker exactly once for the life of
    this server process (st.cache_resource - shared across all browser
    sessions, survives every script rerun). This is a startup health
    check: it happens right now, at first page load, not lazily deferred
    to a user's first message - and it retries with a fresh process
    (start_worker's own backoff) rather than silently deferring failure
    discovery to a random later request. Returns None if every attempt
    failed; the sidebar below surfaces that immediately.
    """
    return piper_worker.start_worker({preset: str(path) for preset, path in VOICE_MODELS.items()})


_piper = None
speak_enabled = False
voice_preset = "Male"

# Real bug, found after the first port to this repo: get_piper_worker()
# calls ctx.Process(...).start() (spawn context). On spawn, the child
# process's bootstrap unconditionally re-executes this whole script via
# runpy to reconstruct __main__ (needed so pickled objects referencing
# __main__ can be resolved) - and that reconstruction reaches this exact
# line again, tries to start ANOTHER child, and trips multiprocessing's
# own "not fully bootstrapped yet" guard. This is real - reproduced with
# a genuine Playwright-driven browser session against a real `streamlit
# run` process, not just a test-harness artifact (see the conversation).
#
# multiprocessing.freeze_support() - the fix Python's own error message
# suggests - is a no-op on macOS/Linux (confirmed via source: it only
# does anything on Windows with a frozen/frozen-to-.exe interpreter), so
# it would not have fixed this here.
#
# What actually distinguishes the two contexts: Streamlit's real
# ScriptRunner always execs this script as a module literally named
# "__main__" (every rerun); multiprocessing's spawn reconstruction runs
# it via runpy.run_path(main_path, run_name="__mp_main__") - a different
# name. So a plain __name__ check reliably tells the two apart, even
# though Streamlit's own docs warn "if __name__ == '__main__'" doesn't
# mean what it normally would in a Streamlit script (their concern is
# that it's True on every real rerun, not just a literal first launch -
# a different issue from what's being guarded against here).
if __name__ == "__main__":
    _piper = get_piper_worker()

    if _piper is None:
        st.sidebar.warning(
            "\U0001f507 Voice unavailable this session - failed to start after 3 attempts. "
            "See server log for details."
        )
    else:
        speak_enabled = st.sidebar.checkbox("\U0001f50a Speak responses aloud", value=False)
        voice_preset = st.sidebar.radio(
            "Voice", ["Female", "Male", "Bot"], index=1, horizontal=True
        )  # Male default - arbitrary, all three presets are equally reliable now
           # that voice loading happens once at startup instead of per call.


def speak(text: str, preset: str = "Female") -> None:
    """Generates audio via the persistent Piper worker (piper_worker.py)
    and plays it back through st.audio(autoplay=True). Only called once
    per NEW assistant reply (never from the message-history replay loop
    above, except via the explicit Replay button which calls this
    directly too), so old messages aren't re-spoken automatically on
    every Streamlit rerun.

    Voice is a bonus feature layered on top of the real agent, and it
    must never be able to take the text answer down with it. That's why
    synthesis still runs in a separate OS process (the persistent
    worker) rather than in-process: a broken local espeak-ng install
    fails at the native (C) layer with a hard process abort, not a
    raised Python exception - nothing in-process could catch that. If
    the worker process dies or hangs, only it is affected; this
    (Streamlit) process keeps running normally. On any failure (worker
    unavailable, dead, timed out, or a caught exception inside it), this
    logs server-side, shows a small inline note, and returns - the text
    response above it has already rendered and stays intact either way.
    This is the same graceful-degradation safety net as before, just
    talking to a long-lived worker instead of spawning a fresh one.
    """
    # Strip markdown table pipes/asterisks so speech doesn't read out
    # formatting characters - a light cleanup, not a full markdown parser.
    clean = re.sub(r"[|#*_`]", " ", text)

    with st.spinner("\U0001f50a Generating audio..."):
        status, payload = piper_worker.synthesize(_piper, clean, preset)

    if status != "ok":
        logging.warning(
            "TTS synthesis failed (preset=%s, reason=%s) - skipping audio for this turn",
            preset,
            status,
        )
        st.caption("\U0001f507 Voice unavailable for this response.")
        return

    st.audio(payload, format="audio/wav", autoplay=True)


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

for idx, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "assistant":
            # Replays the ALREADY-GENERATED text via speak() directly - no
            # client.query()/run_turn() call, so zero new LLM or tool calls.
            # This is deliberately separate from typing "read that again"
            # into chat, which goes through the full agent pipeline and can
            # produce a genuinely different answer (verified: it re-examined
            # the data and added new caveats rather than repeating itself) -
            # that's a real follow-up, this button is just "say it again."
            if st.button("\U0001f501 Replay audio", key=f"replay_{idx}"):
                speak(message["content"], preset=voice_preset)

prompt = st.chat_input("Ask about an airport...")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Working..."):
            events = get_event_loop().run_until_complete(run_turn(get_client(), prompt))

            # Persist the result immediately, still inside the spinner
            # block. A session_state write is a plain dict operation, not a
            # Streamlit command, so it can't itself be caught by a pending
            # rerun checkpoint (Streamlit only checks for those on st.*
            # calls - see the diagnosis in the conversation). Previously
            # this append happened after the render loop below; if a rerun
            # was requested while run_until_complete() was in flight (e.g.
            # the "Speak responses aloud" toggle clicked mid-query), the
            # pending exception fired at the next st.* call - which used to
            # be BEFORE this append ran - so a fully-generated answer was
            # silently discarded. Appending here, before any further st.*
            # call, means the answer survives even through an interrupted
            # run: if the exception does fire right after this line, the
            # next script run's history-render loop (above) just picks the
            # already-stored message up and displays it normally.
            reply_text = "\n\n".join(
                event["content"] for event in events if event["type"] == "text"
            )
            st.session_state.messages.append({"role": "assistant", "content": reply_text})

        for event in events:
            if event["type"] == "tool_call":
                st.caption(f"\U0001f527 called `{event['name']}`  `{event['input']}`")
            elif event["type"] == "text":
                st.markdown(event["content"])

    if speak_enabled and reply_text:
        speak(reply_text, preset=voice_preset)
