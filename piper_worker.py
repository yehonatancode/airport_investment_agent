"""Persistent Piper TTS worker process.

Replaces the old spawn-per-call approach: instead of launching a fresh
subprocess (and reloading all 3 voice models) for every single synthesis
request, one worker process is started once, loads all 3 models a single
time, then serves requests over a pair of queues for the rest of the app's
life. This removes per-call spawn/model-load overhead as a variable in the
intermittent timeout - and, per the root-cause investigation (see the
conversation / DESIGN.md), the actual espeak-ng bug corrupts a fixed-size
stack path buffer at native-library load time in a way that appears to
depend on that process's memory layout at launch (consistent with
espeak-ng issue OHF-Voice/piper1-gpl#272 plus our own observed
intermittency, which the upstream report does not show). A persistent
worker only pays that risk once per worker (at startup, health-checked and
retried below) rather than once per user request.

Protocol: the parent sends (text, preset) tuples on request_queue; the
worker replies with (status, payload) on response_queue. A single
sentinel value of None on request_queue tells the worker to shut down.
Startup uses the same pattern for its own one-off health check: the
worker's first message back is ("ready", None) once all voices are loaded,
or ("error", <reason>) if model loading raised a normal Python exception.
If the worker crashes outright (native abort - the actual observed bug),
nothing arrives, and the parent's start_worker() detects that itself via
a timeout and process-liveness check, exactly like the old per-call code
did.
"""

import io
import logging
import multiprocessing
import threading
import time
import wave

_lock = threading.Lock()


def _piper_worker_main(request_queue, response_queue, model_paths: dict[str, str]) -> None:
    """Entry point for the persistent worker subprocess. Must stay a
    module-level function (not a closure) so it's picklable for spawn."""
    from piper import PiperVoice, SynthesisConfig

    voices = {}
    try:
        for preset, path in model_paths.items():
            voices[preset] = PiperVoice.load(path)
    except Exception as exc:  # noqa: BLE001 - reporting back to the parent
        response_queue.put(("error", f"model load failed: {exc}"))
        return

    response_queue.put(("ready", None))

    while True:
        request = request_queue.get()
        if request is None:
            break
        text, preset = request
        try:
            voice = voices[preset]
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                if preset == "Bot":
                    voice.synthesize_wav(text, wav_file, syn_config=SynthesisConfig(length_scale=0.9))
                else:
                    voice.synthesize_wav(text, wav_file)
            response_queue.put(("ok", buffer.getvalue()))
        except Exception as exc:  # noqa: BLE001 - reporting back to the parent
            response_queue.put(("error", str(exc)))


def start_worker(
    model_paths: dict[str, str],
    health_check_timeout: float = 30.0,
    max_attempts: int = 3,
    backoff_seconds: float = 1.5,
):
    """Starts the persistent worker and blocks until it either reports
    ("ready", None) after loading all voices, or fails. On failure
    (Python exception during load, a native crash, or a hang), retries
    with a fresh process up to max_attempts times - a fresh process gets
    a fresh memory layout, which per the root-cause hypothesis above gives
    each attempt an independent chance of avoiding the underlying bug.

    Returns (proc, request_queue, response_queue) on success, or None if
    every attempt failed - callers should treat None as "voice disabled
    for this session" and surface that immediately, not per-request.
    """
    ctx = multiprocessing.get_context("spawn")
    for attempt in range(1, max_attempts + 1):
        request_queue = ctx.Queue()
        response_queue = ctx.Queue()
        proc = ctx.Process(
            target=_piper_worker_main, args=(request_queue, response_queue, model_paths)
        )
        proc.start()

        try:
            status, _ = response_queue.get(timeout=health_check_timeout)
        except Exception:
            status = "timeout"

        if status == "ready" and proc.is_alive():
            logging.info("Piper worker ready (attempt %d/%d)", attempt, max_attempts)
            return proc, request_queue, response_queue

        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=5)
        logging.warning(
            "Piper worker startup attempt %d/%d failed (status=%s)%s",
            attempt, max_attempts, status,
            " - retrying" if attempt < max_attempts else " - giving up",
        )
        if attempt < max_attempts:
            time.sleep(backoff_seconds * attempt)

    return None


def synthesize(worker, text: str, preset: str, timeout: float = 15.0):
    """Sends one synthesis request to an already-started worker and waits
    for the response. worker is the tuple returned by start_worker(), or
    None (voice unavailable this session - returns immediately without
    touching any process). The lock serializes access to the shared
    worker across concurrent Streamlit sessions in the same server
    process - synthesis requests are rare/short enough that serializing
    them is a reasonable, simple tradeoff over per-request routing.

    Returns (status, payload) - status "ok" with wav bytes, or a failure
    string ("unavailable", "worker_dead", "timeout", or the exception text
    from the worker) with payload None. Never raises - this is the final
    safety net callers rely on for graceful degradation.
    """
    if worker is None:
        return "unavailable", None
    proc, request_queue, response_queue = worker
    with _lock:
        if not proc.is_alive():
            return "worker_dead", None
        request_queue.put((text, preset))
        try:
            return response_queue.get(timeout=timeout)
        except Exception:
            return "timeout", None
