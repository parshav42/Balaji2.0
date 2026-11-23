#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then echo 'Install Python 3.10+.'; exit 1; fi
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install -r requirements.txt
printf '\nBALAJI model: %s\n' "${BALAJI_MODEL:-qwen3:1.7b}"
printf 'Make sure Ollama is running and the model is downloaded.\n'
python -m uvicorn server:app --host 127.0.0.1 --port 8000
