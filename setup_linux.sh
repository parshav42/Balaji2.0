#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"

usage() { printf 'Usage: %s [--check | --start]\n' "$0"; }
mode=${1:-setup}
if [[ $# -gt 1 ]]; then usage; exit 2; fi
case "$mode" in
  --check|--start|setup) ;;
  *) usage; exit 2 ;;
esac

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  printf 'Linux distribution: %s\n' "${PRETTY_NAME:-unknown}"
fi
command -v python3 >/dev/null || { printf 'Install Python 3.10+ and venv first.\n'; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3,10))' || { printf 'Python 3.10+ is required.\n'; exit 1; }

for program in ollama ffmpeg tailscale; do
  if command -v "$program" >/dev/null; then
    printf '%s: found\n' "$program"
  else
    printf '%s: missing (see README)\n' "$program"
  fi
done
if command -v ollama >/dev/null; then
  model="${BALAJI_MODEL:-qwen3:1.7b}"
  if ollama list | awk -v model="$model" '$1==model { found=1 } END { exit !found }'; then
    printf 'Ollama model: %s available\n' "$model"
  else
    printf 'Ollama model: %s unavailable or service offline\n' "$model"
  fi
fi
if command -v tailscale >/dev/null; then
  if tailscale status --json | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin).get("BackendState") != "Running")' 2>/dev/null; then
    printf 'Tailscale: connected to a tailnet\n'
  else
    printf 'Tailscale: sign in and check its service before configuring private access\n'
  fi
fi
whisper_root="${BALAJI_WHISPER_ROOT:-$HOME/whisper.cpp}"
whisper_bin="${BALAJI_WHISPER_BIN:-$whisper_root/build/bin/whisper-cli}"
whisper_model="${BALAJI_WHISPER_MODEL:-$whisper_root/models/ggml-tiny.en.bin}"
if [[ -x "$whisper_bin" && -r "$whisper_model" ]]; then printf 'Whisper: executable and model ready\n'; else printf 'Whisper: check executable and model paths\n'; fi
voice_dir="${BALAJI_VOICE_DIR:-voices}"
voice="${BALAJI_PIPER_VOICE:-en_US-lessac-medium}"
if [[ -r "$voice_dir/$voice.onnx" && -r "$voice_dir/$voice.onnx.json" ]]; then printf 'Piper: model and config ready\n'; else printf 'Piper: missing voice files\n'; fi

if [[ -x .venv/bin/python ]] && .venv/bin/python -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("piper") is None)'; then
  printf 'Piper: Python package present\n'
else
  printf 'Piper: run setup to install the Python package\n'
fi
if [[ "$mode" == '--check' ]]; then exit 0; fi
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-voice.txt
.venv/bin/python - <<'PY'
from zoneinfo import ZoneInfo
from balaji import config, desktop, gmail

config.validate_ollama_url()
config.validate_remote_host()
ZoneInfo(config.TIMEZONE)
desktop.projects()
if config.GMAIL_CLIENT_FILE:
    gmail.client_settings()
PY
.venv/bin/python -m pytest -q
if command -v node >/dev/null; then node --test tests/test_composers.cjs; fi
.venv/bin/python -m balaji.maintenance --migrate
if [[ "$mode" == '--start' ]]; then exec ./run_linux.sh; fi
