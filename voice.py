"""BALAJI push-to-talk offline voice for Linux.

Audio path: ALSA arecord -> ffmpeg WAV conversion -> whisper.cpp ->
Ollama-backed BALAJI Python agent -> Piper -> ALSA aplay.

Requires model downloads only once. Speech execution is local afterwards.
Run: python voice.py
Diagnostics: python voice.py --say 'Hello' / python voice.py --listen-only
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from balaji import agent, config, storage

def approve_in_terminal(name: str, args: dict) -> bool:
    print(f"\n[Approval required] {name}: {args}")
    return input("Allow? [y/N]: ").strip().lower() in {"y", "yes"}

MODEL = config.MODEL

HERE = Path(__file__).resolve().parent
WHISPER_ROOT = Path(os.environ.get("BALAJI_WHISPER_ROOT", str(Path.home() / "whisper.cpp"))).expanduser()
WHISPER_BIN = Path(os.environ.get("BALAJI_WHISPER_BIN", str(WHISPER_ROOT / "build/bin/whisper-cli"))).expanduser()
WHISPER_MODEL = Path(os.environ.get("BALAJI_WHISPER_MODEL", str(WHISPER_ROOT / "models/ggml-tiny.en.bin"))).expanduser()
PIPER_VOICE = os.environ.get("BALAJI_PIPER_VOICE", "en_US-lessac-medium")
VOICE_DIR = Path(os.environ.get("BALAJI_VOICE_DIR", str(HERE / "voices"))).expanduser()
AUDIO_BACKEND = os.environ.get("BALAJI_AUDIO_BACKEND", "alsa")
MAX_SPEECH_CHARS = 750


class VoiceSetupError(RuntimeError):
    """One dependency or local audio device is not ready."""


class VoiceProcessError(VoiceSetupError):
    """A local speech subprocess returned an error."""


def ensure_executable(name: str) -> None:
    if shutil.which(name) is None:
        raise VoiceSetupError(f"'{name}' is missing from PATH. Check the Linux setup instructions.")


def execute(args: list[str], *, label: str, timeout: int = 120) -> None:
    try:
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       text=True, errors="replace", timeout=timeout)
    except subprocess.CalledProcessError as e:
        message = (e.stderr or "").strip()[-1600:]
        raise VoiceProcessError(f"{label} failed (exit code {e.returncode}). {message}") from e
    except subprocess.TimeoutExpired as e:
        raise VoiceSetupError(f"{label} timed out. Try a shorter recording or reply.") from e
    except OSError as e:
        raise VoiceSetupError(f"{label} cannot start: {e}") from e


def record_audio(raw_path: Path, seconds: int) -> None:
    if AUDIO_BACKEND == "alsa":
        ensure_executable("arecord")
        # CD format: 44.1 kHz 16-bit stereo, broadly compatible with ALSA defaults.
        cmd = ["arecord", "-q", "-d", str(seconds), "-f", "cd", "-t", "wav", str(raw_path)]
    elif AUDIO_BACKEND == "pulse":
        ensure_executable("ffmpeg")
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "pulse", "-i", "default", "-t", str(seconds), str(raw_path)]
    else:
        raise VoiceSetupError("BALAJI_AUDIO_BACKEND must be 'alsa' or 'pulse'.")
    execute(cmd, label="Microphone recording")
    if not raw_path.is_file() or raw_path.stat().st_size < 100:
        raise VoiceSetupError("Recorded audio is empty. Check your microphone permissions and device.")


def convert_audio(source: Path, destination: Path, *, input_format: str | None = None,
                  max_seconds: int | None = None) -> None:
    ensure_executable("ffmpeg")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-y"]
    if input_format:
        cmd += ["-protocol_whitelist", "file", "-f", input_format]
    cmd += ["-i", str(source), "-map", "0:a:0", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"]
    if max_seconds is not None:
        cmd += ["-t", str(max_seconds + 1)]
    execute([*cmd, str(destination)], label="WAV conversion", timeout=30)
    with wave.open(str(destination), "rb") as wav:
        if wav.getnframes() <= 0 or wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("The recording contains no usable audio.")
        if max_seconds is not None and wav.getnframes() > 16000 * max_seconds:
            raise ValueError(f"Recordings must be {max_seconds} seconds or shorter.")


def transcribe(wav_path: Path, output_stem: Path) -> str:
    if not WHISPER_BIN.is_file():
        raise VoiceSetupError(f"whisper-cli missing: {WHISPER_BIN}. Build whisper.cpp first.")
    if not WHISPER_MODEL.is_file():
        raise VoiceSetupError(f"Whisper model missing: {WHISPER_MODEL}. Download tiny.en first.")
    execute([str(WHISPER_BIN), "-m", str(WHISPER_MODEL), "-f", str(wav_path), "-otxt", "-of", str(output_stem), "-nt"], label="Whisper transcription")
    transcript_file = Path(str(output_stem) + ".txt")
    if not transcript_file.exists():
        raise VoiceSetupError("Whisper finished but did not write the transcript. Test whisper-cli manually.")
    return transcript_file.read_text(encoding="utf-8").strip()


def hear(seconds: int) -> str:
    if not 1 <= seconds <= 15:
        raise ValueError("Recording length must be from 1 to 15 seconds.")
    with tempfile.TemporaryDirectory(prefix="balaji_voice_") as tmp:
        root = Path(tmp)
        raw = root / "raw.wav"
        audio = root / "speech.wav"
        stem = root / "transcript"
        print(f"[Microphone] Speak now for {seconds} seconds...")
        record_audio(raw, seconds)
        convert_audio(raw, audio)
        return transcribe(audio, stem)


def synthesize(message: str, destination: Path) -> None:
    """Generate a WAV with the configured local Piper voice."""
    voice_model = VOICE_DIR / f"{PIPER_VOICE}.onnx"
    voice_config = VOICE_DIR / f"{PIPER_VOICE}.onnx.json"
    if not voice_model.is_file() or not voice_config.is_file():
        raise VoiceSetupError(f"Piper voice files missing in {VOICE_DIR}. Download {PIPER_VOICE}.")
    execute([sys.executable, "-m", "piper", "-m", str(voice_model), "--config", str(voice_config),
             "-f", str(destination), "--", message], label="Piper synthesis")
    try:
        with wave.open(str(destination), "rb") as wav:
            if wav.getnframes() > 0:
                return
    except (OSError, EOFError, wave.Error) as exc:
        raise VoiceSetupError("Piper could not produce playable audio. Check the Piper installation and voice files.") from exc
    raise VoiceSetupError("Piper produced no speech. Check the voice model and configuration.")


def speak(message: str) -> None:
    message = " ".join(message.split())[:MAX_SPEECH_CHARS]
    if not message:
        return
    ensure_executable("aplay")
    with tempfile.TemporaryDirectory(prefix="balaji_speech_") as tmp:
        speech = Path(tmp) / "reply.wav"
        synthesize(message, speech)
        execute(["aplay", "-q", str(speech)], label="Speaker playback")


def main() -> None:
    parser = argparse.ArgumentParser(description="BALAJI offline voice assistant for Linux")
    parser.add_argument("--seconds", type=int, default=5, help="push-to-talk recording duration 1..15 seconds")
    parser.add_argument("--listen-only", action="store_true", help="record and transcribe; do not call Ollama")
    parser.add_argument("--say", metavar="TEXT", help="test Piper TTS without Ollama or microphone")
    args = parser.parse_args()
    try:
        if args.say is not None:
            speak(args.say)
            print("Piper speaking test finished.")
            return
        if args.listen_only:
            print("Transcription:", hear(args.seconds) or "<empty audio>")
            return
        storage.init_db()
        print(f"BALAJI voice | Model: {MODEL} | backend: {AUDIO_BACKEND}")
        print("Press Enter to record. Type q then Enter to quit. Mutating tools still require keyboard approval.")
        while True:
            command = input("\n[Enter: talk | q: quit] > ").strip().lower()
            if command in {"q", "quit", "exit"}:
                break
            question = hear(args.seconds)
            if not question:
                print("[Whisper] No speech detected. Try again with a quieter room or longer duration.")
                continue
            print("You:", question)
            reply = agent.ask(question, "voice", approve_in_terminal)["answer"]
            print("BALAJI:", reply)
            speak(reply)
    except (VoiceSetupError, OSError, ValueError) as e:
        print(f"\n[Setup problem] {e}", file=sys.stderr)
        raise SystemExit(1) from e
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    main()
