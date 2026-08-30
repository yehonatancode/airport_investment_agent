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
import re
import wave
from pathlib import Path

import streamlit as st
from claude_agent_sdk import ClaudeSDKClient
from piper import PiperVoice

from cli import build_agent_options, run_turn

st.set_page_config(page_title="Airport Investment Agent", page_icon="\U0001f6eb")
st.title("Airport Investment Agent")
st.caption(
    "Ask about airport congestion, growth trends, or which airports look like "
    "strong candidates for terminal expansion. Backed by real BTS data and a "
    "deterministic scoring engine - see DESIGN.md for methodology."
)

speak_enabled = st.sidebar.checkbox("\U0001f50a Speak responses aloud", value=True)
voice_preset = st.sidebar.radio("Voice", ["Female", "Male", "Bot"], index=0, horizontal=True)

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


@st.cache_resource
def _load_piper_voice(preset: str) -> PiperVoice:
    # Cached process-wide (st.cache_resource, not per-session state) since
    # loading an ONNX model is the slow part (~0.5s) - synthesis itself is
    # fast once loaded. Missing model files raise here with a clear path,
    # rather than failing silently on first speak() call.
    return PiperVoice.load(str(VOICE_MODELS[preset]))


def speak(text: str, preset: str = "Female") -> None:
    """Generates audio server-side via Piper and plays it back through
    st.audio(autoplay=True). Only called once per NEW assistant reply
    (never from the message-history replay loop above, except via the
    explicit Replay button which calls this directly too), so old messages
    aren't re-spoken automatically on every Streamlit rerun.
    """
    # Strip markdown table pipes/asterisks so speech doesn't read out
    # formatting characters - a light cleanup, not a full markdown parser.
    clean = re.sub(r"[|#*_`]", " ", text)

    # Scoped tightly around the synthesis call only (model load + WAV
    # generation - the real, measured cost, ~0.5-2s depending on response
    # length) - not around st.audio() itself, so the spinner clears the
    # instant the player is ready rather than lingering over it, and never
    # wraps message rendering or tool-call captions (those already happen
    # before speak() is called at both call sites - the main turn handler
    # and the Replay button).
    with st.spinner("\U0001f50a Generating audio..."):
        voice = _load_piper_voice(preset)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            if preset == "Bot":
                # danny-low is already the deliberately robotic choice (a
                # genuinely lower-quality model, not a parameter hack on a
                # good one). Piper's SynthesisConfig has no native pitch
                # control - only length_scale (speech rate) is available to
                # nudge further; a lower value shortens phoneme duration,
                # i.e. speaks faster.
                from piper import SynthesisConfig

                voice.synthesize_wav(
                    clean, wav_file, syn_config=SynthesisConfig(length_scale=0.9)
                )
            else:
                voice.synthesize_wav(clean, wav_file)
        audio_bytes = buffer.getvalue()

    st.audio(audio_bytes, format="audio/wav", autoplay=True)


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

        reply_text_parts = []
        for event in events:
            if event["type"] == "tool_call":
                st.caption(f"\U0001f527 called `{event['name']}`  `{event['input']}`")
            elif event["type"] == "text":
                st.markdown(event["content"])
                reply_text_parts.append(event["content"])

    reply_text = "\n\n".join(reply_text_parts)
    st.session_state.messages.append({"role": "assistant", "content": reply_text})
    if speak_enabled and reply_text:
        speak(reply_text, preset=voice_preset)
