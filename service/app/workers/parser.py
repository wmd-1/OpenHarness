"""Output file parser — locate and probe video output from oh CLI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from subprocess import PIPE, run

from app.config import settings


class OutputNotFoundError(Exception):
    """Raised when no mp4 output file can be located."""


@dataclass
class VideoMeta:
    duration_seconds: float | None = None
    resolution: str | None = None
    fps: int | None = None
    file_size_bytes: int | None = None


# Patterns for extracting the output file path from oh terminal output.
# Priority 1: Chinese "**输出文件:** `path`"
_RE_ZH = re.compile(r"\*\*输出文件[:：]\*\*\s*`([^`]+\.mp4)`")
# Priority 2: English "**Output:** `path`"
_RE_EN = re.compile(r"\*\*[Oo]utput[:：]\*\*\s*`([^`]+\.mp4)`")
# Also match plain "Output file:" variants
_RE_PLAIN = re.compile(r"[Oo]utput\s+file[:：]\s*`?([^\s`]+\.mp4)`?")


def locate_output_file(stdout: str, workspace: Path) -> Path:
    """Find the mp4 output file produced by oh / hyperframes.

    Strategy:
    1. Regex-match the terminal output for an explicit file path.
    2. Fallback: ``rglob('*.mp4')`` in workspace, pick the newest by mtime.
    3. If still not found → raise :class:`OutputNotFoundError`.
    """
    for pattern in (_RE_ZH, _RE_EN, _RE_PLAIN):
        m = pattern.search(stdout)
        if m:
            candidate = Path(m.group(1))
            if not candidate.is_absolute():
                candidate = workspace / candidate
            if candidate.exists():
                return candidate

    # Fallback: find newest mp4 in workspace
    mp4s = sorted(workspace.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if mp4s:
        return mp4s[0]

    raise OutputNotFoundError(
        f"No mp4 output found in oh stdout or workspace {workspace}"
    )


def probe_mp4(path: Path) -> VideoMeta:
    """Use ffprobe to extract duration, resolution, fps, and file size."""
    meta = VideoMeta(file_size_bytes=path.stat().st_size if path.exists() else None)

    try:
        result = run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(path),
            ],
            stdout=PIPE,
            stderr=PIPE,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return meta

        data = json.loads(result.stdout)

        # Duration from format
        fmt = data.get("format", {})
        if "duration" in fmt:
            meta.duration_seconds = round(float(fmt["duration"]), 3)

        # Resolution & fps from first video stream
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                w = stream.get("width")
                h = stream.get("height")
                if w and h:
                    meta.resolution = f"{w}x{h}"
                # fps can be in r_frame_rate like "30/1"
                rfr = stream.get("r_frame_rate", "")
                if "/" in rfr:
                    num, den = rfr.split("/", 1)
                    if int(den) != 0:
                        meta.fps = int(int(num) / int(den))
                break
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return meta
