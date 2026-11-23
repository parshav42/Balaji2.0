# BALAJI — Local AI Personal Assistant (Linux v1.0)

A **free, local-first, portfolio-ready AI agent**. Ollama powers reasoning and tool calling, FastAPI provides a local browser dashboard, SQLite stores conversations/memories/tasks, and optional whisper.cpp + Piper provide **push-to-talk offline voice**. No paid LLM API is needed.

> **Scope:** BALAJI v1.0 is a complete *local personal-assistant MVP*, not a complete Iron-Man/Jarvis operating-system controller. It does not have wake-word detection, unrestricted desktop control, browser automation, email access or cloud synchronization. Microphone and speaker capabilities require local setup.

## Included

- **Local chat:** Ollama `/api/chat`, selectable model, bounded tool-call loop, input validation.
- **Tools:** local time, remember/search notes, create/list/complete tasks, search documents.
- **Explicit approval:** changes to tasks and memory require terminal confirmation or browser Approve/Reject.
- **Persistence:** SQLite messages (per chat), tasks, notes, approvals, document chunks.
- **Local documents:** upload text/Markdown/PDF, chunk + retrieve, provide source markers. Keyword search works with zero embedding models; optional Ollama embeddings add semantic search.
- **Web dashboard:** streams Ollama replies as they arrive. Includes conversation history, task and memory lists, document upload, and approval controls.
- **Browser voice:** push-to-talk with a recording timer and level meter, transcript preview, optional auto-send, and Piper playback of completed replies. The dashboard includes Stop, Mute, and volume controls.
- **Linux terminal voice:** ALSA or PulseAudio recording feeds the same local Whisper and Piper installations. Terminal and browser each use the agent’s existing approval flow.
- **Tests/CI:** mocked LLM and speech models. Automated tests run without model downloads or audio hardware.

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

You can also launch with `./run_linux.sh`. For browser voice, install the speech dependencies and models below in the same environment used by the web server.

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

Install FFmpeg for browser and terminal voice. Install `git`, `cmake` and `build-essential` to build whisper.cpp. The browser uses its configured microphone and speakers. The `alsa-utils` package and the commands below are for terminal audio diagnostics.

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

| Setting | Default |
|---|---|
| `BALAJI_WHISPER_ROOT` | `~/whisper.cpp` |
| `BALAJI_WHISPER_BIN` | `$BALAJI_WHISPER_ROOT/build/bin/whisper-cli` |
| `BALAJI_WHISPER_MODEL` | `$BALAJI_WHISPER_ROOT/models/ggml-tiny.en.bin` |
| `BALAJI_VOICE_DIR` | `voices/` inside the project |
| `BALAJI_PIPER_VOICE` | `en_US-lessac-medium`, with matching `.onnx` and `.onnx.json` files in the voice directory |
| `BALAJI_AUDIO_BACKEND` | `alsa`. Set `pulse` for terminal recording through PipeWire/PulseAudio. |

Export overrides in the shell that starts BALAJI. For terminal voice, run `python voice.py` and press Enter to record for a fixed duration. Use `python voice.py --help` for options.

### Use Voice Mode in the browser

1. Open **http://127.0.0.1:8000**, turn **Voice Mode** on, and click the microphone beside the message field.
2. Select **Allow** when the browser prompts for microphone access. If permission is already blocked, open the site controls next to the address bar, allow **Microphone**, and reload.
3. Say “Balaji, explain AI agents” and click **Stop recording**. After Whisper finishes, the transcript appears in the dashboard. Turn **Auto-send** off to edit the transcript before pressing **Send**.
4. Read the streamed reply as it arrives. Piper reads up to 750 characters of the completed answer. If the browser blocks autoplay, select **Play reply**.
5. Use **Stop generation** to interrupt the stream, or **Stop playback** to stop the audio. Adjust **Volume** or select **Mute**. Voice Mode starts off on each page load, and recording starts only when you click the microphone.

Requests to change tasks or memories still require clicking **Approve** or **Reject**. Approval details are displayed in the dashboard. The voice endpoints are `POST /api/voice/transcribe` (multipart `file`, WebM/Ogg/MP4/WAV, up to 8 MiB and 60 seconds) and `POST /api/voice/speak` (JSON `{"text":"Hello"}`, up to 750 characters, WAV response).

## 6. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q balaji server.py balaji.py voice.py
```

The test suite mocks Ollama, Whisper and Piper. Use the following checklist for physical devices, browser permissions and the installed model’s behavior.

### Manual browser checklist

- [ ] With Voice Mode off, send a typed message and verify that tokens arrive incrementally and the reply is silent.
- [ ] Enable Voice Mode and allow the microphone. Verify **Listening**, the timer, and the level meter. Stop recording and check **Transcribing**, the transcript, streamed text, and audio playback.
- [ ] Disable Auto-send and edit the transcript before submitting. Try Stop during generation, Stop during speech, Mute, and volume adjustment. Verify **Play reply** works if autoplay is blocked.
- [ ] Speak a task or memory request. Verify the data changes only after explicit approval. Reload to verify conversation history and pending approvals are still available.
- [ ] Try microphone denial, an empty recording, and a mobile-sized viewport. Confirm the dashboard shows a clear error, recovers, and keeps the tasks, memories, and documents accessible.

## 7. Project layout

```text
BALAJI_Complete_Linux_v1/
├── balaji/
│   ├── agent.py            # guarded tool calls / approvals
│   ├── ollama_client.py    # Ollama HTTP API
│   ├── config.py           # environment options
│   ├── storage.py          # SQLite persistence
│   ├── tools.py            # strict Python tool allowlist
│   ├── voice_api.py        # local speech endpoints and upload validation
│   └── rag.py              # ingestion + retrieval
├── web/                    # local chat dashboard and browser voice controls
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

- **Run only on localhost** (`127.0.0.1`). The REST API **does not implement login/authentication**. Public hosting requires authentication, HTTPS, CSRF protection, and per-user authorization.
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
| Browser microphone denied or unavailable | Open `http://127.0.0.1:8000`, allow the microphone in the site settings, and check the selected Linux input device. A current Chromium or Firefox browser can help if recording format support fails. |
| Empty audio | Check that the browser’s level meter moves when you speak. For terminal recording, verify `arecord` or try `BALAJI_AUDIO_BACKEND=pulse`. |
| Whisper executable/model missing | Build whisper.cpp, download the model, and set paths. |
| Browser stays silent | Select **Play reply** if shown. Check Mute, Volume, site sound settings, and the Linux output device. If synthesis fails, check the Piper model files and install `requirements-voice.txt` in the server’s virtual environment. |
| PDF shows no chunks | Use a digital-text PDF or convert scanned pages to searchable text. |

Official project docs: [Ollama](https://docs.ollama.com/), [whisper.cpp](https://github.com/ggml-org/whisper.cpp), [Piper](https://github.com/OHF-Voice/piper1-gpl).

## Job portfolio positioning

Explain why you used an explicit allowlist, why mutating tools require approvals, how state is stored in SQLite, how retrieval works with/without embeddings, and what your unit tests do **and don't** validate. This is more convincing than claiming a fully autonomous Jarvis.
