"""Refuse to run the suite while another pytest is alive on this machine.

Two concurrent pytest runs of this repo deadlock each other (both wedge at 0%
CPU mid-suite -- shared model/HF caches), and a wedged stray from a killed run
keeps re-triggering it: the next suite silently hangs for its whole timeout
instead of failing. Detecting the stray at session start turns a 10-minute
silent hang into an instant, actionable error.

Kept as a plain module (not inside conftest.py) so the detection logic is
unit-testable: see tests/unit/test_concurrency_guard.py.
"""

from __future__ import annotations

import os
import subprocess

# A process is "a pytest run" if its command line invokes pytest itself --
# `python -m pytest ...` or a pytest console script -- or is a shell wrapper
# carrying such an invocation. Plain mentions of the word (grep pytest,
# an editor with a test file open) don't match.
_PYTEST_MARKERS = ("-m pytest", "bin/pytest")


def foreign_pytest_pids(rows: list[tuple[int, int, str]], my_pid: int) -> list[int]:
    """PIDs of pytest processes that are neither this run nor its ancestors
    (the shell that launched us has 'pytest' in its command string) nor its
    descendants (workers/subprocesses this run spawned).

    `rows` are (pid, ppid, command) tuples, e.g. parsed from `ps`."""
    parent_of = {pid: ppid for pid, ppid, _ in rows}

    def ancestry(pid: int) -> set[int]:
        seen: set[int] = set()
        while pid in parent_of and parent_of[pid] not in seen:
            pid = parent_of[pid]
            seen.add(pid)
        return seen

    own_tree = ancestry(my_pid) | {my_pid}
    foreign = []
    for pid, _ppid, command in rows:
        if not any(marker in command for marker in _PYTEST_MARKERS):
            continue
        if pid in own_tree or my_pid in ancestry(pid):
            continue
        foreign.append(pid)
    return sorted(foreign)


def running_foreign_pytest_pids() -> list[int]:
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=10
    ).stdout
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return foreign_pytest_pids(rows, my_pid=os.getpid())
