#!/usr/bin/env python3
"""
Live mic -> faster-whisper (CUDA) -> WebSocket captions.

Captures the microphone via ffmpeg (ALSA), runs streaming-ish transcription
with faster-whisper on the GPU, and broadcasts caption updates as JSON over a
WebSocket. The slide deck connects to it and renders a minimalist overlay.

Message shape (JSON):
  {"type": "interim", "text": "...partial line being spoken..."}
  {"type": "final",   "text": "...committed line..."}
  {"type": "clear"}

Env overrides:
  MIC_DEVICE   ALSA device         (default: plughw:2,0)
  WHISPER_MODEL model size         (default: small.en)
  WS_HOST / WS_PORT                (default: 127.0.0.1 / 8765)
  COMPUTE_TYPE float16|int8_float16 (default: float16)
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque

import numpy as np

try:
    import websockets
    from faster_whisper import WhisperModel
except ImportError as e:
    sys.exit(f"Missing dependency: {e}. Run ./run.sh which installs into a venv.")

# ----- config -----------------------------------------------------------------
MIC_DEVICE   = os.environ.get("MIC_DEVICE", "plughw:2,0")
MODEL_NAME   = os.environ.get("WHISPER_MODEL", "small.en")
COMPUTE_TYPE = os.environ.get("COMPUTE_TYPE", "float16")
WS_HOST      = os.environ.get("WS_HOST", "127.0.0.1")
WS_PORT      = int(os.environ.get("WS_PORT", "8765"))

SAMPLE_RATE  = 16000
FRAME_MS     = 100                                  # audio read granularity
FRAME_BYTES  = int(SAMPLE_RATE * FRAME_MS / 1000) * 2   # s16le mono

# segmentation / cadence
SILENCE_RMS      = 0.012     # below this = silence (normalized float)
SILENCE_COMMIT_S = 0.8       # trailing silence that ends an utterance
INTERIM_EVERY_S  = 0.5       # re-transcribe cadence while speaking
MAX_UTTER_S      = 14.0      # force-commit very long utterances
MIN_SPEECH_S     = 0.25      # ignore blips shorter than this

# ----- hallucination filtering ------------------------------------------------
import re

# Phrases Whisper commonly emits on silence/noise. Dropped only when they are
# the ENTIRE output, so they never censor real speech mid-sentence.
_HALLUCINATIONS = {
    "you", "thank you", "thank you.", "thanks for watching",
    "thanks for watching!", "thank you for watching", "please subscribe",
    "bye", "bye.", "okay", "ok", ".", "..", "...", "so", "oh", "uh", "um",
    "i'm going to do it.", "subtitles by the amara.org community",
}


def _sanitize(text: str) -> str:
    if not text:
        return ""
    norm = re.sub(r"\s+", " ", text).strip()
    low = norm.lower().strip(" .!?,-")
    if low in _HALLUCINATIONS or len(low) <= 1:
        return ""
    # collapse hard repetition ("BLEH! BLEH! BLEH!" -> junk); if one token
    # repeats 4+ times in a row, treat the whole thing as non-speech noise.
    words = re.findall(r"\w+", low)
    if words:
        run = maxrun = 1
        for a, b in zip(words, words[1:]):
            run = run + 1 if a == b else 1
            maxrun = max(maxrun, run)
        if maxrun >= 4:
            return ""
        # a single distinct word screamed repeatedly
        if len(set(words)) == 1 and len(words) >= 3:
            return ""
    return norm


# ----- shared broadcast plumbing ---------------------------------------------
_clients = set()
_loop = None  # asyncio loop, set in main


def _broadcast(msg: dict):
    """Thread-safe push to all websocket clients."""
    if _loop is None:
        return
    data = json.dumps(msg)
    asyncio.run_coroutine_threadsafe(_fan_out(data), _loop)


async def _fan_out(data: str):
    dead = []
    for ws in list(_clients):
        try:
            await ws.send(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


async def _handler(ws):
    _clients.add(ws)
    try:
        await ws.wait_closed()
    finally:
        _clients.discard(ws)


# ----- audio capture ----------------------------------------------------------
def _spawn_ffmpeg():
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "alsa", "-i", MIC_DEVICE,
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=FRAME_BYTES)


def _read_exact(stream, n):
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


# ----- transcription loop -----------------------------------------------------
def transcribe_loop(model: WhisperModel):
    proc = _spawn_ffmpeg()
    print(f"[mic] ffmpeg capturing {MIC_DEVICE} @ {SAMPLE_RATE}Hz", flush=True)

    utter = deque()            # float32 frames for the current utterance
    speaking = False
    silence_run = 0.0
    speech_dur = 0.0
    last_interim = 0.0
    last_text = ""

    def transcribe(audio: np.ndarray) -> str:
        segments, _ = model.transcribe(
            audio, language="en", beam_size=1,
            vad_filter=True,                       # Silero VAD drops non-speech
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,
            no_speech_threshold=0.55,
            log_prob_threshold=-1.0,
            temperature=0.0,
        )
        kept = []
        for s in segments:
            # drop low-confidence / non-speech segments (hallucination guard)
            if getattr(s, "no_speech_prob", 0.0) > 0.6:
                continue
            if getattr(s, "avg_logprob", 0.0) < -1.1:
                continue
            kept.append(s.text.strip())
        return _sanitize(" ".join(kept).strip())

    while True:
        raw = _read_exact(proc.stdout, FRAME_BYTES)
        if raw is None:
            err = proc.stderr.read().decode(errors="ignore") if proc.stderr else ""
            print(f"[mic] ffmpeg ended. {err}", flush=True)
            time.sleep(1.0)
            proc = _spawn_ffmpeg()
            continue

        frame = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
        is_speech = rms >= SILENCE_RMS
        dt = FRAME_MS / 1000.0

        if is_speech:
            if not speaking:
                speaking = True
                speech_dur = 0.0
                last_interim = 0.0
            utter.append(frame)
            speech_dur += dt
            silence_run = 0.0
        elif speaking:
            utter.append(frame)   # keep trailing silence in-buffer for context
            silence_run += dt

        now_len = len(utter) * dt

        # emit interim while actively speaking
        if speaking and (speech_dur - last_interim) >= INTERIM_EVERY_S \
                and speech_dur >= MIN_SPEECH_S:
            audio = np.concatenate(list(utter))
            text = transcribe(audio)
            last_interim = speech_dur
            if text and text != last_text:
                last_text = text
                _broadcast({"type": "interim", "text": text})

        # commit on trailing silence or max length
        end = speaking and (silence_run >= SILENCE_COMMIT_S or now_len >= MAX_UTTER_S)
        if end:
            if speech_dur >= MIN_SPEECH_S:
                audio = np.concatenate(list(utter))
                text = transcribe(audio)
                if text:
                    _broadcast({"type": "final", "text": text})
            utter.clear()
            speaking = False
            silence_run = 0.0
            speech_dur = 0.0
            last_text = ""


async def main():
    global _loop
    _loop = asyncio.get_running_loop()

    print(f"[asr] loading faster-whisper '{MODEL_NAME}' on CUDA ({COMPUTE_TYPE})...",
          flush=True)
    model = WhisperModel(MODEL_NAME, device="cuda", compute_type=COMPUTE_TYPE)
    # warm up the CUDA graph so the first real utterance isn't slow
    model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")
    print("[asr] model ready.", flush=True)

    threading.Thread(target=transcribe_loop, args=(model,), daemon=True).start()

    async with websockets.serve(_handler, WS_HOST, WS_PORT):
        print(f"[ws] captions live at ws://{WS_HOST}:{WS_PORT}", flush=True)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[exit] bye", flush=True)
