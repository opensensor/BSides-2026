# Live captions (GPU)

Real-time microphone → **faster-whisper on CUDA** → WebSocket → minimalist
caption overlay on the slide deck. For audio-impaired attendees.

## Run

```bash
./run.sh                 # first run builds .venv + installs deps
```

Then open `deck/index.html` in the browser. Captions appear at the bottom.
Press **`c`** to toggle them on/off. A small bottom-right badge shows status
(`CC live` = connected, `CC …` = server not up, `CC off` = toggled off).

## Tuning (env vars)

```bash
WHISPER_MODEL=medium.en ./run.sh     # more accurate, still real-time on the 4070
MIC_DEVICE=plughw:1,0   ./run.sh     # pick a different mic (see: arecord -l)
COMPUTE_TYPE=int8_float16 ./run.sh   # lower VRAM if needed
```

- Default model is `small.en` (low latency). The RTX 4070 (8 GB) runs
  `medium.en` comfortably in real time if you want higher accuracy.
- **Find your mic:** `arecord -l` → card N, device M → `MIC_DEVICE=plughw:N,M`.
  For a venue feed / USB lav, set this to that device before the talk.

## How it works

- `transcribe.py` reads 16 kHz mono PCM from `ffmpeg` (ALSA), segments speech
  with an RMS gate, and transcribes on the GPU. It emits `interim` updates
  while you speak and a `final` when you pause.
- Hallucination guards (Silero VAD + `no_speech_prob`/`avg_logprob` thresholds
  + a blocklist + repetition collapse) keep silence/noise from producing the
  classic phantom captions ("Thanks for watching!", "you", "BLEH BLEH…").
- `../deck/captions-overlay.js` connects to `ws://127.0.0.1:8765`, renders the
  overlay, and auto-reconnects if the server isn't up yet.

## GPU note

The driver was the only fix needed — the running kernel (`7.0.0-15`) had no
matching NVIDIA module. Installed `linux-modules-nvidia-580-open-7.0.0-15-generic`
and `modprobe nvidia`. No CUDA toolkit required: CTranslate2 pulls the CUDA 12
cuBLAS/cuDNN runtime as pip wheels (works under the CUDA 13 driver). To survive
future kernel upgrades, install `nvidia-dkms-580-open` so the module rebuilds
automatically.
```
