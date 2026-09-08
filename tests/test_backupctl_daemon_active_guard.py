"""Regression tests for backupctl's daemon-active guard
(_assert_no_active_daemon), which refuses a manual `run`/`retry` while
supervisor.sh/backupd.py are live -- two writers against one state DB and
one bucket is a corruption risk. It only refuses or logs; it must never
kill the daemon itself, which is an explicit operator decision.
"""

import os
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BACKUPCTL_PATH = Path(__file__).resolve().parent.parent / "bin" / "backupctl"


def _load_backupctl():
    loader = SourceFileLoader("backupctl_under_test_daemon_guard", str(BACKUPCTL_PATH))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_no_active_daemon_proceeds(monkeypatch):
    backupctl = _load_backupctl()
    monkeypatch.setattr(backupctl, "_active_daemon_processes", lambda: [])
    monkeypatch.setattr(backupctl, "_held_daemon_locks", lambda: [])
    assert backupctl._assert_no_active_daemon(allow=False) is True


def test_active_daemon_refuses_without_override(monkeypatch):
    backupctl = _load_backupctl()
    monkeypatch.setattr(
        backupctl, "_active_daemon_processes", lambda: [(123, "python app/backupd.py")]
    )
    monkeypatch.setattr(backupctl, "_held_daemon_locks", lambda: [])
    assert backupctl._assert_no_active_daemon(allow=False) is False


def test_active_daemon_proceeds_with_explicit_override(monkeypatch):
    backupctl = _load_backupctl()
    monkeypatch.setattr(
        backupctl, "_active_daemon_processes", lambda: [(123, "python app/backupd.py")]
    )
    monkeypatch.setattr(backupctl, "_held_daemon_locks", lambda: [])
    assert backupctl._assert_no_active_daemon(allow=True) is True


def test_never_kills_the_daemon(monkeypatch):
    backupctl = _load_backupctl()
    calls = []
    monkeypatch.setattr(os, "kill", lambda *a, **k: calls.append(("kill", a, k)))
    monkeypatch.setattr(
        backupctl, "_active_daemon_processes", lambda: [(123, "python app/backupd.py")]
    )
    monkeypatch.setattr(backupctl, "_held_daemon_locks", lambda: [])
    backupctl._assert_no_active_daemon(allow=False)
    backupctl._assert_no_active_daemon(allow=True)
    assert calls == []


def test_unrelated_supervisor_processes_do_not_false_positive():
    """Found live 2026-09-07: the patterns were the bare basenames
    "supervisor.sh"/"backupd.py", so `/workspace/.pod-persist/bin/
    rtfm-supervisor.sh --daemon` -- RTFM's supervisor, nothing to do with
    backups -- matched, and every `backupctl run`/`retry`/`abandon` on this
    pod was refused with a message naming a process the operator must not
    kill. A guard that cries wolf trains the operator into
    --allow-daemon-active, which is the one flag that permits the
    concurrent-writer corruption the guard exists to prevent.
    """
    backupctl = _load_backupctl()
    unrelated = (
        "bash /workspace/.pod-persist/bin/rtfm-supervisor.sh --daemon",
        "/usr/bin/python3 /opt/other-project/backupd.py",
        "tail -f supervisor.sh.log",
    )
    for cmdline in unrelated:
        assert not any(
            pat in cmdline for pat in backupctl.DAEMON_PROCESS_PATTERNS
        ), cmdline


def test_locks_are_authoritative_when_the_command_line_does_not_match(monkeypatch):
    """The other half of the same 2026-09-07 finding, and the dangerous
    direction: a supervisor started as `bash bin/supervisor.sh` (relative,
    which is what the operator actually types) matches no absolute-path
    pattern at all. Command-line matching can only ever be a hint; the
    flock the daemon itself holds is the real signal, so a held lock alone
    must refuse.
    """
    backupctl = _load_backupctl()
    monkeypatch.setattr(backupctl, "_active_daemon_processes", lambda: [])
    monkeypatch.setattr(
        backupctl, "_held_daemon_locks", lambda: ["daemon (state/backupd.lock is locked)"]
    )
    assert backupctl._assert_no_active_daemon(allow=False) is False
    assert backupctl._assert_no_active_daemon(allow=True) is True


def test_lock_is_held_reports_free_lock_as_free(tmp_path):
    """Exercised against a real file and the real flock syscall."""
    backupctl = _load_backupctl()
    assert backupctl._lock_is_held(tmp_path / "nobody-holds-this.lock") is False


def test_self_pid_never_matches():
    """Exercised for real against the live /proc, not mocked -- this is
    exactly the self-matching bug (a pattern in the issuing shell's own
    command line matching itself) that has bitten this project twice."""
    backupctl = _load_backupctl()
    found = backupctl._active_daemon_processes()
    assert os.getpid() not in {pid for pid, _ in found}
