"""Integration tests for ``run_oh`` with a *real* subprocess.

These spawn an actual child process (a small stand-in for the ``oh`` CLI) so we
can prove the cancellation path in #2 really tears down the whole process group
(parent + its children, e.g. chrome), not just the immediate process. The mocked
worker tests in ``test_worker.py`` cover the state machine; this file covers the
OS-level kill behavior that the mocks cannot.

POSIX-only: ``run_oh`` relies on ``os.setsid`` / ``os.killpg`` which do not exist
on Windows. Skipped there (and in any non-POSIX environment).
"""

import os
import signal
import stat
import textwrap
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("posix")

from app.workers.runner import run_oh


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    """Write an executable python script with a shebang on line 1.

    The shebang MUST be the very first line or the kernel refuses to exec the
    file (``Exec format error``). We prepend it explicitly rather than relying
    on the dedented body starting with it.
    """
    p = tmp_path / name
    p.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


# Spawns a child ``sleep 600`` (stands in for chrome) and then sleeps forever.
# Records both pids to --pidfile (JSON) so the test can confirm the WHOLE group
# (oh + its child) was torn down. Note: the abort flag must flip True only after
# a short delay (see the test) -- run_oh's watchdog kills immediately when
# is_aborted() is already True, which would terminate the dummy before it writes
# its pid file.
ABORT_DUMMY = """\
import sys, time, subprocess, os, json
pidfile = None
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "--pidfile" and i + 1 < len(args):
        pidfile = args[i + 1]
child = subprocess.Popen(["sleep", "600"])
if pidfile:
    with open(pidfile, "w") as f:
        json.dump({"dummy": os.getpid(), "child": child.pid}, f)
time.sleep(600)
"""

# Ignores SIGTERM so the abort/watchdog path alone cannot kill it; only the
# overall timeout -> SIGKILL fallback should terminate it.
IGNORE_TERM_DUMMY = """\
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(600)
"""

# Exits cleanly on its own after emitting a marker to stdout.
EXIT_OK_DUMMY = """\
import sys
sys.stdout.write("done-mark")
sys.stdout.flush()
import time
time.sleep(0.2)
"""


def test_run_to_completion_captures_stdout(tmp_path):
    """A normally-exiting oh subprocess yields exit_code 0 and its stdout."""
    oh = _write_script(tmp_path, "oh_ok.py", EXIT_OK_DUMMY)
    result = run_oh(prompt="make video", cwd=tmp_path, oh_bin=str(oh))
    assert result.exit_code == 0
    assert "done-mark" in result.stdout


def _process_is_runnable(pid: int) -> bool:
    """True only if ``pid`` exists AND is still executing (runnable/sleeping).

    A process killed by ``killpg`` but not yet reaped by its parent shows up as
    a *zombie* (state ``Z``); ``os.kill(pid, 0)`` still reports it as present,
    which is why a naive liveness check gives false positives. We inspect
    ``/proc/<pid>/stat`` so a zombie (or a missing process) counts as stopped.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return False
    # Format: "pid (comm) state ppid ..." -- state is right after the ')'.
    rparen = stat.rindex(")")
    state = stat[rparen + 2]  # skip ')' and the following space
    return state in ("R", "S", "D")


def test_abort_kills_process_group(tmp_path):
    """When is_aborted flips True, run_oh must SIGTERM the whole group, killing
    both the oh process AND its spawned child (no orphan chrome)."""
    oh = _write_script(tmp_path, "oh_abort.py", ABORT_DUMMY)
    pidfile = tmp_path / "pids.json"

    # Flip the abort flag a little after spawn so the dummy has time to record
    # its pids first; run_oh's watchdog kills immediately once is_aborted() is
    # True, which would otherwise win the race against the dummy's startup.
    aborted = {"v": False}
    timer = threading.Timer(0.5, lambda: aborted.__setitem__("v", True))
    timer.daemon = True
    timer.start()
    try:
        result = run_oh(
            prompt="x",
            cwd=tmp_path,
            oh_bin=str(oh),
            extra_args=["--pidfile", str(pidfile)],
            is_aborted=lambda: aborted["v"],
        )
    finally:
        timer.cancel()

    # Killed by signal, not a clean exit.
    assert result.exit_code != 0
    assert result.exit_code < 0

    # The dummy must have recorded both pids before it was killed.
    assert pidfile.exists(), "dummy oh did not record its pids"
    import json

    pids = json.loads(pidfile.read_text())
    # Both the oh process and its spawned child must be gone -- no orphaned
    # chrome left behind by a canceled task.
    for label, pid in (("dummy", pids["dummy"]), ("child", pids["child"])):
        deadline = time.time() + 5
        while time.time() < deadline:
            if not _process_is_runnable(pid):
                break  # stopped (zombie or already reaped)
            time.sleep(0.1)
        else:
            pytest.fail(f"orphan {label} process (pid {pid}) still running after cancellation")


def test_timeout_kills_process_group(tmp_path):
    """If oh ignores SIGTERM and outlives the wall-clock timeout, run_oh must
    escalate to SIGKILL so the task can never hang the worker forever."""
    oh = _write_script(tmp_path, "oh_ignore.py", IGNORE_TERM_DUMMY)
    result = run_oh(
        prompt="x",
        cwd=tmp_path,
        oh_bin=str(oh),
        timeout=1,  # short so the wait() timeout path triggers the SIGKILL fallback
    )
    assert result.exit_code != 0
    # The wait() timeout path sends SIGTERM (ignored) then SIGKILL after 10s.
    assert result.exit_code == -signal.SIGKILL
