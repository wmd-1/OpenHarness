"""Tests for the output file parser module."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.workers.parser import OutputNotFoundError, VideoMeta, locate_output_file, probe_mp4


# ---- locate_output_file tests ----


class TestLocateOutputFile:
    """Test suite for locate_output_file."""

    def test_chinese_output_pattern(self, tmp_path: Path):
        """Should match Chinese '**输出文件:** `path`' pattern."""
        mp4 = tmp_path / "output.mp4"
        mp4.write_bytes(b"\x00\x00\x00" * 100)
        stdout = f"一些日志\n**输出文件:** `{mp4}`\n更多日志"
        result = locate_output_file(stdout, tmp_path)
        assert result == mp4

    def test_english_output_pattern(self, tmp_path: Path):
        """Should match English '**Output:** `path`' pattern."""
        mp4 = tmp_path / "result.mp4"
        mp4.write_bytes(b"\x00\x00\x00" * 100)
        stdout = f"Some logs\n**Output:** `{mp4}`\nMore logs"
        result = locate_output_file(stdout, tmp_path)
        assert result == mp4

    def test_plain_output_file_pattern(self, tmp_path: Path):
        """Should match 'Output file: path' pattern."""
        mp4 = tmp_path / "video.mp4"
        mp4.write_bytes(b"\x00\x00\x00" * 100)
        stdout = f"Logs\nOutput file: {mp4}\nDone"
        result = locate_output_file(stdout, tmp_path)
        assert result == mp4

    def test_relative_path_resolution(self, tmp_path: Path):
        """Should resolve relative paths against workspace."""
        mp4 = tmp_path / "renders" / "output.mp4"
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"\x00\x00\x00" * 100)
        stdout = "**输出文件:** `renders/output.mp4`"
        result = locate_output_file(stdout, tmp_path)
        assert result == mp4

    def test_fallback_rglob_newest(self, tmp_path: Path):
        """Should find the newest mp4 via rglob when no regex match."""
        import time

        old = tmp_path / "old.mp4"
        old.write_bytes(b"\x00" * 10)
        time.sleep(0.05)
        new = tmp_path / "new.mp4"
        new.write_bytes(b"\x00" * 10)
        stdout = "No output pattern here"
        result = locate_output_file(stdout, tmp_path)
        assert result == new

    def test_no_mp4_raises_error(self, tmp_path: Path):
        """Should raise OutputNotFoundError when no mp4 exists."""
        stdout = "No output at all"
        with pytest.raises(OutputNotFoundError):
            locate_output_file(stdout, tmp_path)

    def test_regex_match_but_file_missing_falls_back(self, tmp_path: Path):
        """If regex matches a path but file doesn't exist, should try next pattern or fallback."""
        mp4 = tmp_path / "actual.mp4"
        mp4.write_bytes(b"\x00" * 10)
        stdout = f"**输出文件:** `/nonexistent/ghost.mp4`\nOutput file: {mp4}"
        result = locate_output_file(stdout, tmp_path)
        assert result == mp4

    def test_chinese_colon_variant(self, tmp_path: Path):
        """Should match Chinese full-width colon variant."""
        mp4 = tmp_path / "test.mp4"
        mp4.write_bytes(b"\x00" * 10)
        stdout = f"**输出文件：** `{mp4}`"
        result = locate_output_file(stdout, tmp_path)
        assert result == mp4


# ---- probe_mp4 tests ----


class TestProbeMp4:
    """Test suite for probe_mp4."""

    def test_file_size_recorded(self, tmp_path: Path):
        """Should record file size even if ffprobe is unavailable."""
        mp4 = tmp_path / "video.mp4"
        content = b"\x00" * 12345
        mp4.write_bytes(content)
        meta = probe_mp4(mp4)
        assert meta.file_size_bytes == 12345

    @patch("app.workers.parser.run")
    def test_probe_with_mock_ffprobe(self, mock_run, tmp_path: Path):
        """Should parse ffprobe JSON output correctly."""
        mp4 = tmp_path / "video.mp4"
        mp4.write_bytes(b"\x00" * 5000)

        ffprobe_output = {
            "format": {"duration": "30.5"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "r_frame_rate": "30/1",
                }
            ],
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(ffprobe_output)
        mock_run.return_value = mock_result

        meta = probe_mp4(mp4)
        assert meta.duration_seconds == 30.5
        assert meta.resolution == "1920x1080"
        assert meta.fps == 30
        assert meta.file_size_bytes == 5000

    @patch("app.workers.parser.run")
    def test_probe_ffprobe_fails_gracefully(self, mock_run, tmp_path: Path):
        """Should handle ffprobe failure gracefully."""
        mp4 = tmp_path / "video.mp4"
        mp4.write_bytes(b"\x00" * 2000)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        meta = probe_mp4(mp4)
        assert meta.file_size_bytes == 2000
        assert meta.duration_seconds is None
        assert meta.resolution is None

    @patch("app.workers.parser.run")
    def test_probe_non_integer_fps(self, mock_run, tmp_path: Path):
        """Should handle fractional frame rates correctly."""
        mp4 = tmp_path / "video.mp4"
        mp4.write_bytes(b"\x00" * 1000)

        ffprobe_output = {
            "format": {"duration": "60.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1280,
                    "height": 720,
                    "r_frame_rate": "24000/1001",
                }
            ],
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(ffprobe_output)
        mock_run.return_value = mock_result

        meta = probe_mp4(mp4)
        assert meta.fps == int(24000 / 1001)  # 23
        assert meta.resolution == "1280x720"

    def test_probe_nonexistent_file(self):
        """Should handle missing file gracefully."""
        meta = probe_mp4(Path("/nonexistent/file.mp4"))
        assert meta.file_size_bytes is None
