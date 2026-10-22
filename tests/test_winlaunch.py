"""Behavior coverage for the winlaunch launch primitive (audit F5/F12).

These spawn real processes through the primitive — the same path the daemon
uses for shell wakes — so they double as end-to-end proof that captured
output, exit codes, env, cwd, timeouts, and whole-tree kills behave.
"""
import os
import subprocess
import sys
import time

import pytest

from galvanize import dispatch, winlaunch
from galvanize.config import Trigger
from galvanize.events import Event


def test_run_captures_output_and_exit_code():
    r = winlaunch.run([sys.executable, "-c",
                       "import sys; print('hi'); sys.stderr.write('bad'); sys.exit(3)"])
    assert r.returncode == 3
    assert r.stdout.startswith("hi")
    assert "bad" in r.stderr
    assert "warning" not in r.stderr  # no _winapi handle-relaxation noise


def test_run_shell_string_and_env():
    r = winlaunch.run("echo %GZ_PROBE%", shell=True,
                      env={**os.environ, "GZ_PROBE": "42"})
    assert r.returncode == 0
    assert "42" in r.stdout


def test_run_respects_cwd(tmp_path):
    r = winlaunch.run([sys.executable, "-c", "import os; print(os.getcwd())"],
                      cwd=str(tmp_path))
    assert r.returncode == 0
    assert os.path.samefile(r.stdout.strip(), str(tmp_path))


def test_run_missing_executable_raises_oserror():
    with pytest.raises(OSError):
        winlaunch.run(["definitely-not-an-exe-gz-probe", "x"])


def test_run_timeout_raises_with_partial_output():
    code = "import sys, time; sys.stdout.write('x' * 70000); sys.stdout.flush(); time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        winlaunch.run([sys.executable, "-c", code], timeout=3)
    assert len(ei.value.output or "") >= 70000  # > pipe buffer, read concurrently


@pytest.mark.skipif(os.name != "nt", reason="Job Object tree-kill semantics")
def test_timeout_kills_whole_tree_not_just_direct_child():
    """Audit F12: cmd/python wrapper times out; the grandchild MUST die with it."""
    mark = "GZTREEKILL7x9"
    child = f"import time; time.sleep(60)  # {mark}"
    parent = ("import subprocess, sys, time; "
              f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)")
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        winlaunch.run([sys.executable, "-c", parent], timeout=2)
    assert time.time() - t0 < 8
    time.sleep(0.5)
    scan = winlaunch.run([
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        "ForEach-Object CommandLine"])
    hits = [l for l in scan.stdout.splitlines()
            if mark in l and "Get-CimInstance" not in l]
    assert not hits, f"orphaned descendants: {hits}"


def test_spawn_detached_starts_surviving_child():
    pid, escaped = winlaunch.spawn_detached(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    assert pid > 0
    assert isinstance(escaped, bool)  # True from a console test run on this box
    os.kill(pid, 0)  # alive; raises if it never came up
    os.kill(pid, 15)  # cleanup (Windows: terminates)


# ------------------------------------------------------- dispatch integration


def test_shell_wake_failure_writes_full_output_file(isolated_homes, tmp_path):
    script = tmp_path / "wake.py"
    script.write_text("import sys\nsys.stderr.write('E' * 2500)\nsys.exit(1)\n",
                      encoding="utf-8")
    trigger = Trigger("wakey", {"type": "emit"},
                      {"kind": "shell",
                       "command": f'"{sys.executable}" "{script}" {{payload}}'})
    event = Event("wakey", "emit", "changed", {})
    ok, detail = dispatch._wake_shell(trigger, event, "p")
    assert not ok
    log = isolated_homes["galvanize"] / "wake_output" / "wakey.log"
    assert log.exists(), detail
    assert "E" * 2500 in log.read_text(encoding="utf-8")  # FULL, not truncated
    assert str(log) in detail
    # F12: the old 400-char budget is gone; details carry a generous tail.
    assert "E" * 401 in detail


def test_shell_wake_timeout_reports_tree_kill(isolated_homes, tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\nprint('hi', flush=True)\ntime.sleep(60)\n",
                      encoding="utf-8")
    trigger = Trigger("slowpoke", {"type": "emit"},
                      {"kind": "shell", "timeout_s": 2,
                       "command": f'"{sys.executable}" "{script}" {{payload}}'})
    event = Event("slowpoke", "emit", "changed", {})
    ok, detail = dispatch._wake_shell(trigger, event, "p")
    assert not ok
    assert "timed out after 2.0s" in detail
    assert "tree killed" in detail
    log = isolated_homes["galvanize"] / "wake_output" / "slowpoke.log"
    assert log.exists()
