import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
import voice


def test_recording_duration_bounds():
    for seconds in (0, -1, 16):
        with pytest.raises(ValueError):
            voice.hear(seconds)


def test_missing_whisper_files():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        with patch.object(voice,'WHISPER_MODEL',root/'missing.bin'):
            with patch.object(voice,'WHISPER_BIN',root/'missing-cli'):
                with pytest.raises(voice.VoiceSetupError):
                    voice.transcribe(root/'audio.wav',root/'out')


def test_empty_speech_skips_synthesis():
    with patch.object(voice,'execute') as run:
        voice.speak('  ')
        run.assert_not_called()
