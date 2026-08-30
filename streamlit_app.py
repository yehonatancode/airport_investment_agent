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
import io
import logging
import multiprocessing
import re
import wave
from pathlib import Path

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

speak_enabled = st.sidebar.checkbox("\U0001f50a Speak responses aloud", value=False)
voice_preset = st.sidebar.radio(
    "Voice", ["Female", "Male", "Bot"], index=1, horizontal=True
)  # Male default. Piper synthesis has an intermittent subprocess timeout
   # observed across all three presets (not Female-specific, not fixed by
   # this default) - see DESIGN.md. Any preset can still hit it; when it
   # does, the existing timeout/crash isolation below handles it gracefully.

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


def _synthesize_worker(model_path: str, text: str, length_scale: float | None, queue) -> None:
    """Runs in a separate OS process (see speak() below). Must stay a
    module-level function, not a closure, so it's picklable for spawn.

    Loads its own PiperVoice rather than reusing a cached one - a loaded
    PiperVoice (wraps a native onnxruntime session) doesn't reliably cross
    a process boundary, so this pays the ~0.5s load cost again every call.
    That's the deliberate tradeoff for real crash isolation (see speak()).
    """
    try:
        from piper import PiperVoice, SynthesisConfig

        voice = PiperVoice.load(model_path)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            if length_scale is not None:
                voice.synthesize_wav(text, wav_file, syn_config=SynthesisConfig(length_scale=length_scale))
            else:
                voice.synthesize_wav(text, wav_file)
        queue.put(("ok", buffer.getvalue()))
    except Exception as exc:  # noqa: BLE001 - reporting back to the parent, not swallowing
        queue.put(("error", str(exc)))


def speak(text: str, preset: str = "Female") -> None:
    """Generates audio server-side via Piper and plays it back through
    st.audio(autoplay=True). Only called once per NEW assistant reply
    (never from the message-history replay loop above, except via the
    explicit Replay button which calls this directly too), so old messages
    aren't re-spoken automatically on every Streamlit rerun.

    Voice is a bonus feature layered on top of the real agent, and it must
    never be able to take the text answer down with it. A plain try/except
    around synthesize_wav() was tried first and does NOT work: a broken
    local espeak-ng install fails at the native (C) layer with a hard
    process exit, not a raised Python exception - nothing in-process can
    catch that. So synthesis runs in a separate OS process instead; if
    that child process aborts, only it dies - this (Streamlit) process is
    unaffected and keeps running normally. On any failure (crash, timeout,
    caught exception in the worker), this logs server-side, shows a small
    inline note, and returns - the text response above it has already
    rendered and stays intact either way.
    """
    # Strip markdown table pipes/asterisks so speech doesn't read out
    # formatting characters - a light cleanup, not a full markdown parser.
    clean = re.sub(r"[|#*_`]", " ", text)
    length_scale = 0.9 if preset == "Bot" else None
    model_path = str(VOICE_MODELS[preset])

    with st.spinner("\U0001f50a Generating audio..."):
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()
        proc = ctx.Process(
            target=_synthesize_worker, args=(model_path, clean, length_scale, result_queue)
        )
        proc.start()
        proc.join(timeout=30)

        if proc.is_alive():
            proc.terminate()
            proc.join()
            status, payload = "timeout", None
        elif proc.exitcode != 0:
            # Non-zero (or negative, meaning killed by signal - e.g. a
            # native abort) exit code: the worker process crashed before it
            # could report anything back through the queue.
            status, payload = f"crashed (exit code {proc.exitcode})", None
        else:
            try:
                status, payload = result_queue.get_nowait()
            except Exception:
                status, payload = "no_result_returned", None

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
