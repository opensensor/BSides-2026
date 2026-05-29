#!/usr/bin/env bash
# Set up (once) and launch the live caption server.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
if [ ! -d "$VENV" ]; then
  echo "[setup] creating venv..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install -r requirements.txt
fi

# CTranslate2 finds the pip-installed cuDNN/cuBLAS via LD_LIBRARY_PATH.
# `nvidia` is a namespace package (no __file__); use its search locations.
NV_LIB="$($VENV/bin/python - <<'PY'
import glob, os, importlib.util
spec = importlib.util.find_spec("nvidia")
base = spec.submodule_search_locations[0]
print(":".join(sorted(glob.glob(os.path.join(base, "*", "lib")))))
PY
)"
export LD_LIBRARY_PATH="${NV_LIB}:${LD_LIBRARY_PATH:-}"

# Defaults (override inline, e.g. WHISPER_MODEL=medium.en ./run.sh)
export MIC_DEVICE="${MIC_DEVICE:-plughw:2,0}"
export WHISPER_MODEL="${WHISPER_MODEL:-small.en}"

echo "[run] mic=$MIC_DEVICE model=$WHISPER_MODEL"
exec "$VENV/bin/python" transcribe.py
