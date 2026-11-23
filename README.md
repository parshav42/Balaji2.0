# BALAJI — Local AI Personal Assistant (Linux v1.0)

A **free, local-first, portfolio-ready AI agent**. Ollama powers reasoning and tool calling, FastAPI provides a local browser dashboard, SQLite stores conversations/memories/tasks, and optional whisper.cpp + Piper provide **push-to-talk offline voice**. No paid LLM API is needed.

> **Scope:** BALAJI v1.0 is a complete *local personal-assistant MVP*, not a complete Iron-Man/Jarvis operating-system controller. It does not have wake-word detection, unrestricted desktop control, browser automation, email access or cloud synchronization. Microphone and speaker capabilities require local setup.

## Included

- **Local chat:** Ollama `/api/chat`, selectable model, bounded tool-call loop, input validation.
- **Tools:** local time, remember/search notes, create/list/complete tasks, search documents.
- **Explicit approval:** changes to tasks and memory require terminal confirmation or browser Approve/Reject.
- **Persistence:** SQLite messages (per chat), tasks, notes, approvals, document chunks.
- **Local documents:** upload text/Markdown/PDF, chunk + retrieve, provide source markers. Keyword search works with zero embedding models; optional Ollama embeddings add semantic search.
- **Web UI:** modern single-page dashboard via FastAPI, task/memory lists, document upload, approval controls.
- **Linux voice:** microphone -> ffmpeg -> whisper.cpp -> Ollama -> Piper -> speakers, all offline after setup; requires keyboard approvals.
- **Tests/CI:** mocked LLM tests; no live model, paid API or hardware needed to run automated tests.

## 1. Hardware / model check

```bash
free -h
lscpu | grep -E 'Model name|CPU\(s\)'
df -h ~
```

Small model for an unknown/8GB machine: `qwen3:1.7b`. On very limited hardware try `qwen3:0.6b`, but agent tool-calling may be unreliable. On a 16GB+ computer consider `qwen3.5:4b`. **Do not assume the model fits before checking memory.**

## 2. Ollama (Linux)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:1.7b
ollama list
curl http://127.0.0.1:11434/api/tags
```

Start Ollama if required using `sudo systemctl start ollama` (systemd) or `ollama serve` in a separate terminal. Test with `ollama run qwen3:1.7b` and leave using `/bye`.

## 3. Start BALAJI's web interface

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
cd BALAJI_Complete_Linux_v1
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BALAJI_MODEL=qwen3:1.7b
uvicorn server:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. The dashboard will show chat, notes, tasks and uploaded documents. API schema is at **http://127.0.0.1:8000/docs**.

To run the same agent in terminal mode:

```bash
python balaji.py
```

Examples: “What's the time?”, “Remember I want to practise Python”, “Create a task to revise LangGraph”, “What tasks are pending?”, “Search my knowledge documents for SQLite.”

If BALAJI asks for approval, inspect the operation and choose **Approve** or **Reject**. An approved action is performed once and its result appears in the dashboard. This version does not automatically resume a multi-step agent plan after browser approval; issue a follow-up prompt to continue if necessary.

## 4. Add local documents (RAG)

In the browser, upload `.txt`, `.md`, or `.pdf` (each <= 5 MB, PDF <= 100 pages; scanned PDFs need OCR). Try `examples/knowledge.md` to test retrieval.

With the default `BALAJI_EMBED_MODEL=` unset, search uses lightweight **keyword retrieval**. Semantic retrieval is optional:

```bash
ollama pull nomic-embed-text
export BALAJI_EMBED_MODEL=nomic-embed-text
# Restart server, then upload NEW documents so embeddings are stored.
```

Existing keyword-only documents are not automatically re-embedded. Model answers can be incorrect even with retrieved snippets; verify citations against your original files.

## 5. Offline voice on Linux

### Linux audio prerequisites

```bash
sudo apt install -y git cmake build-essential ffmpeg alsa-utils
arecord -l
arecord -d 5 -f cd -t wav mic-test.wav
aplay mic-test.wav
```

If ALSA's default microphone is unavailable, try a PipeWire/PulseAudio compatibility source:

```bash
sudo apt install -y pulseaudio-utils
ffmpeg -f pulse -i default -t 5 -y mic-test.wav
aplay mic-test.wav
export BALAJI_AUDIO_BACKEND=pulse
```

### Install whisper.cpp (speech to text)

```bash
cd ~
git clone https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
sh ./models/download-ggml-model.sh tiny.en
cmake -B build
cmake --build build -j 2 --config Release
./build/bin/whisper-cli -m models/ggml-tiny.en.bin -f samples/jfk.wav
```

For better English accuracy, use `base.en` and export `BALAJI_WHISPER_MODEL="$HOME/whisper.cpp/models/ggml-base.en.bin"`. `tiny.en` recognizes English; use multilingual models for Hindi/Marathi.

### Install Piper (text to speech)

From the BALAJI project directory, with `.venv` active:

```bash
pip install -r requirements-voice.txt
mkdir -p voices
python -m piper.download_voices --data-dir voices en_US-lessac-medium
python voice.py --say "Hello, I'm Balaji!"
python voice.py --listen-only --seconds 5
python voice.py --seconds 5
```

You can override voice paths with `BALAJI_WHISPER_ROOT`, `BALAJI_WHISPER_BIN`, `BALAJI_WHISPER_MODEL`, `BALAJI_VOICE_DIR`, `BALAJI_PIPER_VOICE`, `BALAJI_AUDIO_BACKEND` (`alsa` or `pulse`). `python voice.py --help` displays options.

**Voice mode is push-to-talk:** press Enter and speak for a short fixed time. The microphone is not always recording. Actions changing local tasks or memories still require keyboard approval.

## 6. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q balaji server.py balaji.py voice.py
```

The test suite mocks Ollama and does not validate actual microphone, speakers, model tool-calling quality or PDF extraction across every kind of file. Complete a real hardware smoke test before claiming a deployed voice demo.

## 7. Project layout

```text
BALAJI_Complete_Linux_v1/
├── balaji/
│   ├── agent.py            # guarded tool calls / approvals
│   ├── ollama_client.py    # Ollama HTTP API
│   ├── config.py           # environment options
│   ├── storage.py          # SQLite persistence
│   ├── tools.py            # strict Python tool allowlist
│   └── rag.py              # ingestion + retrieval
├── web/index.html          # local chat dashboard
├── server.py               # FastAPI and API endpoints
├── balaji.py               # terminal mode
├── voice.py                # offline push-to-talk
├── examples/knowledge.md   # sample knowledge base
├── docs/architecture.md
├── tests/
├── requirements.txt
├── requirements-voice.txt
├── requirements-dev.txt
└── .github/workflows/tests.yml
```

## 8. Security and limitations

- **Run only on localhost** (`127.0.0.1`). The REST API **does not implement login/authentication**. Never bind it to `0.0.0.0`, expose it to the internet, or port-forward it without implementing authentication, CSRF protection and per-user authorization.
- Ollama is restricted to local HTTP addresses by default; no paid API token is needed.
- `.sqlite3` may contain personal data and is gitignored. **Do not upload the database, secrets or private documents to public GitHub.**
- The model may misinterpret requests or make mistakes. The allowlist intentionally does not permit shell execution or arbitrary file manipulation.
- Document contents are untrusted external text; the model is instructed not to follow instructions found inside uploaded files. This is best-effort mitigation, not a complete prompt-injection defense.
- PDF text extraction works for digital text PDFs; scanned documents require OCR, which is not included.
- Tool calling depends on the local model. Small models may fail to pick tools even when they are available.
- Voice is English-first, turn-based and CPU-dependent; responsiveness depends heavily on RAM/CPU.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot reach local Ollama` | Check `ollama list`, `ollama serve` / systemd service and port 11434. |
| Model not found | Run `ollama pull qwen3:1.7b` and set `BALAJI_MODEL` to an installed name. |
| Slow or frozen machine | Try `qwen3:0.6b`, close browser tabs, and disable optional embeddings. |
| Empty audio or missing microphone | Verify `arecord` outside Python; try `BALAJI_AUDIO_BACKEND=pulse`. |
| Whisper executable/model missing | Build whisper.cpp, download the model, and set paths. |
| Piper not speaking | Check your `voices/` files and run `python voice.py --say "test"`. |
| PDF shows no chunks | Use a digital-text PDF or convert scanned pages to searchable text. |

Official project docs: [Ollama](https://docs.ollama.com/), [whisper.cpp](https://github.com/ggml-org/whisper.cpp), [Piper](https://github.com/OHF-Voice/piper1-gpl).

## Job portfolio positioning

Explain why you used an explicit allowlist, why mutating tools require approvals, how state is stored in SQLite, how retrieval works with/without embeddings, and what your unit tests do **and don't** validate. This is more convincing than claiming a fully autonomous Jarvis.
