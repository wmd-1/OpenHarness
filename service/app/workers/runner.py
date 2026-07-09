"""Subprocess wrapper for running the oh CLI."""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen
from threading import Thread
from typing import Callable


@dataclass
class RunResult:
    exit_code: int
    stdout: str


def run_oh(
    prompt: str,
    cwd: Path,
    timeout: int = 900,
    on_log_line: Callable[[str], None] | None = None,
    extra_args: list[str] | None = None,
    is_aborted: Callable[[], bool] | None = None,
    oh_bin: str = "/root/.local/bin/oh",
    headless_shell_path: str = "/opt/chrome-headless-shell-linux64/chrome-headless-shell",
) -> RunResult:
    """Spawn ``oh -p <prompt>`` as a subprocess and collect output.

    Args:
        prompt: The text prompt to pass to ``oh -p``.
        cwd: Working directory (workspace) for the subprocess.
        timeout: Maximum wall-clock seconds before killing the process.
        on_log_line: Callback invoked for each line of combined stdout/stderr.
        extra_args: Additional CLI flags forwarded to ``oh``.
        is_aborted: Optional predicate polled during execution. When it returns
            ``True``, the whole ``oh`` process group is terminated (SIGTERM then
            SIGKILL). Used to honor user cancellation without relying on the
            Celery worker receiving the revoke signal.
        oh_bin: Path to the ``oh`` binary.
        headless_shell_path: Path to chrome-headless-shell binary.

    Returns:
        A :class:`RunResult` with exit code and captured stdout.
    """
    cmd = [
        oh_bin,
        "-p", prompt,
        "--output-format", "text",
        "--permission-mode", "full_auto",
        *(extra_args or []),
    ]

    env = {
        **os.environ,
        "PRODUCER_HEADLESS_SHELL_PATH": headless_shell_path,
        "CHROME_HEADLESS_BIN": headless_shell_path,
    }

    proc = Popen(
        cmd,
        cwd=str(cwd),
        stdout=PIPE,
        stderr=STDOUT,
        text=True,
        bufsize=1,
        env=env,
        preexec_fn=os.setsid,
    )

    # ``setsid`` makes ``oh`` its own session/process-group leader, so
    # ``proc.pid`` is the pgid. Killing it tears down ``oh`` *and* its chrome
    # children (important for cancellation).
    pgid = proc.pid

    def _kill_group(signum: int) -> None:
        try:
            os.killpg(pgid, signum)
        except OSError:
            pass

    def _watchdog() -> None:
        # Poll the abort predicate while the process is alive.
        while proc.poll() is None:
            if is_aborted is not None and is_aborted():
                _kill_group(signal.SIGTERM)
                return
            time.sleep(0.5)

    watchdog_thread = Thread(target=_watchdog, daemon=True)
    watchdog_thread.start()

    lines: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if on_log_line is not None:
                on_log_line(line)

    reader_thread = Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Wait with timeout
    try:
        proc.wait(timeout=timeout)
    except Exception:
        # Kill the entire process group
        _kill_group(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except Exception:
            _kill_group(signal.SIGKILL)
            proc.wait()

    reader_thread.join(timeout=5)
    watchdog_thread.join(timeout=2)

    return RunResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout="".join(lines),
    )
