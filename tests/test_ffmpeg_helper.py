"""Tests for ``audio_processor.run_ffmpeg`` — the single ffmpeg seam."""

from unittest.mock import patch

from audio_processor import run_ffmpeg


def test_uses_bundled_path_when_env_set():
    with patch("audio_processor.subprocess.run") as mock_run, \
         patch.dict("os.environ", {"VINYLFLOW_FFMPEG_PATH": "/opt/bundled/ffmpeg"}, clear=False):
        run_ffmpeg(["-version"], capture_output=True)
        called_cmd = mock_run.call_args.args[0]
        assert called_cmd[0] == "/opt/bundled/ffmpeg"
        assert called_cmd[1:] == ["-version"]


def test_falls_back_to_bare_ffmpeg():
    with patch("audio_processor.subprocess.run") as mock_run, \
         patch.dict("os.environ", {}, clear=True):
        run_ffmpeg(["-version"], capture_output=True)
        assert mock_run.call_args.args[0][0] == "ffmpeg"


def test_text_mode_defaults_utf8_replace():
    with patch("audio_processor.subprocess.run") as mock_run:
        run_ffmpeg(["-version"], capture_output=True)
        kwargs = mock_run.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"


def test_text_false_omits_encoding():
    with patch("audio_processor.subprocess.run") as mock_run:
        run_ffmpeg(["-version"], capture_output=True, text=False)
        kwargs = mock_run.call_args.kwargs
        assert "encoding" not in kwargs
        assert "errors" not in kwargs


def test_caller_kwargs_flow_through():
    with patch("audio_processor.subprocess.run") as mock_run:
        run_ffmpeg(["-version"], capture_output=True, check=True, timeout=42)
        kwargs = mock_run.call_args.kwargs
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 42
        assert kwargs["capture_output"] is True


def test_caller_can_override_encoding():
    with patch("audio_processor.subprocess.run") as mock_run:
        run_ffmpeg(["-version"], capture_output=True, encoding="latin-1")
        kwargs = mock_run.call_args.kwargs
        assert kwargs["encoding"] == "latin-1"
        # errors default still applies because caller didn't override it
        assert kwargs["errors"] == "replace"
