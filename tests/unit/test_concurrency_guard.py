"""Concurrent pytest runs deadlock each other on this machine (shared model/
HF caches; documented repo gotcha): the suite silently hangs mid-run with 0%
CPU instead of failing. The guard makes that failure loud and instant --
another running pytest is detected at session start and the run aborts with
the PIDs to kill.
"""

from concurrency_guard import foreign_pytest_pids


def _ps(*rows):
    """rows: (pid, ppid, command)"""
    return list(rows)


def test_detects_a_foreign_pytest_process():
    rows = _ps(
        (100, 1, "/repo/.venv/bin/python -m pytest tests/ -q"),
        (200, 1, "-zsh"),
    )
    assert foreign_pytest_pids(rows, my_pid=999) == [100]


def test_excludes_itself():
    rows = _ps((999, 1, "/repo/.venv/bin/python -m pytest tests/unit -q"))
    assert foreign_pytest_pids(rows, my_pid=999) == []


def test_excludes_its_own_shell_ancestors():
    """The zsh -c wrapper that launched us has 'pytest' inside its command
    string -- it must not trip the guard."""
    rows = _ps(
        (50, 1, "/bin/zsh -c 'eval .venv/bin/python -m pytest tests/unit -q'"),
        (999, 50, "/repo/.venv/bin/python -m pytest tests/unit -q"),
    )
    assert foreign_pytest_pids(rows, my_pid=999) == []


def test_excludes_its_own_descendants():
    """xdist workers / subprocesses spawned by THIS run are not foreign."""
    rows = _ps(
        (999, 50, "/repo/.venv/bin/python -m pytest tests/unit -q -n 2"),
        (1001, 999, "/repo/.venv/bin/python -m pytest-xdist-worker"),
        (1002, 1001, "/repo/.venv/bin/python -m pytest gw0 worker"),
    )
    assert foreign_pytest_pids(rows, my_pid=999) == []


def test_detects_foreign_run_while_excluding_own_tree():
    rows = _ps(
        (50, 1, "/bin/zsh -c 'pytest tests/unit'"),          # my ancestor
        (999, 50, "/repo/.venv/bin/python -m pytest tests/unit"),  # me
        (300, 1, ".venv/bin/python -m pytest tests/ -q -k lugo"),  # stray from another session
        (301, 300, "/bin/zsh -c '.venv/bin/python -m pytest tests/'"),  # stray's shell wrapper
    )
    assert foreign_pytest_pids(rows, my_pid=999) == [300, 301]


def test_ignores_non_pytest_processes():
    rows = _ps(
        (400, 1, "/usr/bin/vim tests/unit/test_foo.py"),
        (401, 1, "python -m http.server"),
        (402, 1, "grep pytest"),
    )
    assert foreign_pytest_pids(rows, my_pid=999) == []
