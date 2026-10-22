"""Card C3: watchdog task spec + ensure no-op/wedge logic (unit level).

The scheduled task itself cannot be exercised from a unit test, so the
TESTABLE SURFACE is: spec generation (exact argv/quotes), registration
reporting (faked schtasks), and the ensure decision table. Live drills
(real kill, real task fire, real restart) are recorded in the card
comment / handoff, not here.
"""

import json
import os
from pathlib import Path
import time
import pytest

from galvanize import watchdog
from galvanize.paths import galvanize_home, pause_flag_path, shutdown_flag_path

# FIX-CI: the schtasks belt is Windows by design (register/unregister/
# registered short-circuit off NT: systemd/launchd own that role). These
# tests assert the NT argv surface and must not run blindly on POSIX.
NT_ONLY = pytest.mark.skipif(os.name != "nt",
                             reason="Windows scheduled-task belt semantics")


# ------------------------------------------------------------------ spec

def test_task_name_default_vs_profile(monkeypatch):
    monkeypatch.delenv("GALVANIZE_PROFILE", raising=False)
    assert watchdog.task_name() == "GalvanizeWatchdog"
    assert watchdog.task_name("work") == "GalvanizeWatchdog-work"
    monkeypatch.setenv("GALVANIZE_PROFILE", "alt")
    assert watchdog.task_name() == "GalvanizeWatchdog-alt"  # env is the default source


def test_ensure_command_shape_targets_daemon_ensure():
    cmd = watchdog.ensure_command("default")
    assert cmd[:3] == [cmd[0], "-m", "galvanize.cli"]
    assert cmd[-4:] == ["daemon", "ensure", "--profile", "default"]
    if os.name == "nt":
        assert cmd[0].lower().endswith("pythonw.exe"), \
            "scheduled target must be windowless"


def test_tr_string_quotes_exe_keeps_args(monkeypatch):
    monkeypatch.setattr(watchdog, "ensure_command",
                        lambda p=None: [r"C:\Program Files\x\pythonw.exe",
                                        "-m", "galvanize.cli", "daemon", "ensure"])
    tr = watchdog.tr_string(watchdog.ensure_command())
    assert tr.startswith('"C:\\Program Files\\x\\pythonw.exe"')
    assert tr.endswith("-m galvanize.cli daemon ensure")


def test_schtasks_spec_minute_cadence_self_ru_no_password():
    spec = watchdog.schtasks_spec("default", user="jane")
    assert spec[:3] == ["schtasks", "/Create", "/F"]
    i = lambda flag: spec.index(flag)
    assert spec[i("/SC") + 1] == "MINUTE"
    assert spec[i("/MO") + 1] == "1"
    assert spec[i("/RU") + 1] == "jane"
    assert "/RP" not in spec  # feasibility probe: accepted without password, stays Interactive-only
    assert spec[i("/TN") + 1] == "GalvanizeWatchdog"
    # windowless by construction (operator note: console actions pop WT):
    # /TR must be the wscript trampoline, NEVER an interpreter/cmd token
    tr = spec[i("/TR") + 1]
    assert tr.startswith("wscript.exe ")
    assert tr.endswith('.vbs"') or tr.endswith(".vbs")
    assert "pythonw" not in tr and "cmd.exe" not in tr.lower()


def test_write_hidden_launcher_window_style_zero(monkeypatch):
    monkeypatch.setattr(watchdog, "ensure_command",
                        lambda p=None: [r"C:\x y\pythonw.exe", "-m",
                                        "galvanize.cli", "daemon", "ensure"])
    path = watchdog.write_hidden_launcher()
    body = path.read_text(encoding="utf-8")
    assert path == watchdog.hidden_launcher_path()
    runline = [ln for ln in body.splitlines() if ln.startswith("sh.Run")][0]
    assert runline.rstrip().endswith(", 0, False")   # hidden + no-wait
    # the VBS string literal escapes embedded quotes by doubling:
    assert '"""C:\\x y\\pythonw.exe""' in body
    assert 'PYTHONPATH") = ""' in body                # uv-inherit hygiene
    assert "watchdog_hidden" in path.name


def test_write_hidden_launcher_profile_names():
    assert watchdog.hidden_launcher_path("work").name == "watchdog_hidden_work.vbs"
    assert watchdog.hidden_launcher_path("default").name == "watchdog_hidden.vbs"
    assert watchdog.hidden_launcher_path("a b/c").name == "watchdog_hidden_a_b_c.vbs"


# ------------------------------------------------------------------ register (faked schtasks)

class _Run:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


@NT_ONLY
@pytest.mark.parametrize("rc", [0, 1])
def test_register_reports_both_ways(monkeypatch, rc):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Run(rc, "" if rc == 0 else "ERROR: Access is denied.")
    monkeypatch.setattr(watchdog.winlaunch, "run", fake_run)
    monkeypatch.setattr(watchdog, "ensure_command", lambda p=None: ["x.exe", "a"])
    ok, lines = watchdog.register()
    assert calls and calls[0][:2] == ["schtasks", "/Create"]
    assert watchdog.hidden_launcher_path().exists(), "register writes its own vbs"
    if rc == 0:
        # battery gate must be cleared unconditionally after create
        assert any(c[0] == "powershell" for c in calls)
        assert ok and any("every minute" in ln for ln in lines)
    else:
        assert not ok and any("logon-only" in ln for ln in lines), \
            "failure must name the weaker guarantee, never stay silent"


@NT_ONLY
def test_unregister_removes_task_and_vbs(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog.winlaunch, "run",
                        lambda cmd, **kw: calls.append(cmd) or _Run(0))
    path = watchdog.write_hidden_launcher()
    ok, lines = watchdog.unregister()
    assert ok and path.exists() is False
    assert [c[1] for c in calls] == ["/End", "/Delete"]


@NT_ONLY
def test_registered_uses_query_and_caches(monkeypatch):
    watchdog._QUERY_CACHE.clear()
    calls = []
    monkeypatch.setattr(watchdog, "task_name", lambda p=None: "GW-test")
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Run(0)
    monkeypatch.setattr(watchdog.winlaunch, "run", fake_run)
    assert watchdog.registered() is True
    assert watchdog.registered() is True  # second call served from cache
    assert len(calls) == 1 and calls[0][:3] == ["schtasks", "/Query", "/TN"]


# ------------------------------------------------------------------ ensure decision table

def _mk(home, **fields):
    p = home / "supervisor.json"
    p.write_text(json.dumps({"supervisor_pid": 0, "updated": time.time(),
                             "state": "ready", **fields}), encoding="utf-8")
    return p


class _Probe:
    def __init__(self):
        self.forced, self.spawned = [], 0

    def force(self, snap):
        self.forced.append(snap)

    def spawn(self):
        self.spawned += 1
        return 99991, True


def test_ensure_noop_when_snapshot_fresh():
    pr = _Probe()
    res = watchdog.ensure(snapshot=lambda: {"supervisor_pid": 4, "state": "ready"},
                          raw=lambda: {}, force=pr.force, spawn=pr.spawn)
    assert res["action"] == "noop"
    assert pr.spawned == 0 and pr.forced == []


def test_ensure_noop_leaves_no_log_line():
    watchdog.ensure(snapshot=lambda: {"state": "ready"}, raw=lambda: {},
                    force=lambda s: None, spawn=lambda: (1, True))
    assert not (galvanize_home() / "watchdog.log").exists()


def test_ensure_respects_pause_marker():
    pause_flag_path().touch()
    res = watchdog.ensure(snapshot=lambda: None, raw=lambda: None,
                          force=lambda s: None, spawn=lambda: (1, True))
    assert res["action"] == "paused"


def test_ensure_shutdown_flag_without_snapshot_is_paused():
    shutdown_flag_path().touch()
    res = watchdog.ensure(snapshot=lambda: None, raw=lambda: None,
                          force=lambda s: None, spawn=lambda: (1, True))
    assert res["action"] == "paused"


def test_ensure_starts_when_down(monkeypatch):
    pr = _Probe()
    res = watchdog.ensure(snapshot=lambda: None, raw=lambda: None,
                          force=pr.force, spawn=pr.spawn)
    assert res["action"] == "started" and "absent" in res["detail"]
    assert pr.spawned == 1


def test_ensure_clears_corpse_then_starts():
    pr = _Probe()
    corpse = {"supervisor_pid": 0, "updated": time.time(), "state": "ready"}
    res = watchdog.ensure(snapshot=lambda: None, raw=lambda: corpse,
                          force=pr.force, spawn=pr.spawn)
    assert res["action"] == "started" and "corpse" in res["detail"]
    assert pr.forced == [corpse] and pr.spawned == 1


def test_ensure_backs_off_within_legit_window(monkeypatch):
    # live pid, stale status, but inside the max-backoff window: hands off
    monkeypatch.setenv("GALVANIZE_WATCHDOG_WEDGE_S", "45")
    pr = _Probe()
    raw = {"supervisor_pid": os.getpid(), "updated": time.time() - 10,
           "state": "respawning"}
    res = watchdog.ensure(snapshot=lambda: None, raw=lambda: raw,
                          force=pr.force, spawn=pr.spawn)
    assert res["action"] == "backing-off"
    assert pr.spawned == 0 and pr.forced == []


def test_ensure_kills_and_restarts_wedged_supervisor():
    pr = _Probe()
    raw = {"supervisor_pid": os.getpid(), "updated": time.time() - 10_000,
           "state": "respawning"}
    res = watchdog.ensure(snapshot=lambda: None, raw=lambda: raw,
                          force=pr.force, spawn=pr.spawn)
    assert res["action"] == "restarted"
    assert pr.forced == [raw] and pr.spawned == 1
    assert "wedged" in res["detail"]


def test_ensure_surfaces_session_bound_spawn():
    pr = _Probe()
    res = watchdog.ensure(snapshot=lambda: None, raw=lambda: None,
                          force=lambda s: None, spawn=lambda: (7, False))
    assert res["action"] == "started" and "session-bound" in res["detail"]


def test_ensure_spawn_failure_is_failed():
    def boom():
        raise OSError("no child for you")
    res = watchdog.ensure(snapshot=lambda: None, raw=lambda: None,
                          force=lambda s: None, spawn=boom)
    assert res["action"] == "failed"


# ------------------------------------------------------------------ persistence surface

@NT_ONLY
def test_persistence_summary_shapes(monkeypatch):
    monkeypatch.setattr(watchdog, "registered", lambda p=None, **k: True)
    import galvanize.autostart as a
    monkeypatch.setattr(a, "_startup_lnk",
                        lambda: galvanize_home() / "Galvanize.lnk")
    s = watchdog.persistence_summary()
    assert s["watchdog_task"] and not s["startup_lnk"]
    assert "60" in s["note"] or "only" in s["note"]
    (galvanize_home() / "Galvanize.lnk").touch()
    s = watchdog.persistence_summary()
    assert "watchdog + .lnk" == s["note"]


# ------------------------------------------------------------------ card C6: console-free entry
#
# CoS field note: the venv-STUB pythonw re-execs the base console
# interpreter; the WT pseudoconsole outlives the brief tick (19 zombie
# frames in ~20 min). The fix is launcher_plan(): run the BASE install's
# pythonw (GUI subsystem, no stub) with PYTHONPATH into the app venv,
# plus /Enable so a stopgap `schtasks /change /disable` never survives
# `daemon install`.

def _fake_runtime_venv(tmp_path, monkeypatch):
    venv = tmp_path / "rv"
    base = tmp_path / "basecpython"
    base.mkdir()
    (base / "pythonw.exe").write_bytes(b"MZ fake")
    (base / "python.exe").write_bytes(b"MZ fake")
    (venv / "Lib" / "site-packages" / "galvanize").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = {base}\nversion = 3.11.15\n", encoding="utf-8")
    monkeypatch.setenv("GALVANIZE_RUNTIME_VENV", str(venv))
    return venv, base


def test_launcher_plan_fallback_when_no_runtime_venv(monkeypatch):
    monkeypatch.delenv("GALVANIZE_RUNTIME_VENV", raising=False)
    monkeypatch.setattr(watchdog, "ensure_command",
                        lambda p=None: ["stub.exe", "x", "supervise"])
    argv, env, cwd, free = watchdog.launcher_plan()
    assert argv[0] == "stub.exe" and not free
    assert env == {"PYTHONPATH": ""}


@pytest.mark.skipif(os.name != "nt", reason="nt launcher layout")
def test_launcher_plan_targets_base_pythonw_not_the_stub(tmp_path, monkeypatch):
    venv, base = _fake_runtime_venv(tmp_path, monkeypatch)
    argv, env, cwd, free = watchdog.launcher_plan("default")
    assert free, "resolved base interpreter must be reported console-free"
    assert Path(argv[0]) == base / "pythonw.exe", \
        "entry must be the BASE pythonw: the venv stub re-exec is the leak"
    assert "-s" in argv and argv[:3] == [argv[0], "-s", "-m"]
    assert argv[-4:] == ["daemon", "ensure", "--profile", "default"]
    assert env["PYTHONPATH"] == str(venv / "Lib" / "site-packages")
    assert cwd == str(venv)


@pytest.mark.skipif(os.name != "nt", reason="nt launcher layout")
def test_vbs_runs_console_free_plan(tmp_path, monkeypatch):
    venv, base = _fake_runtime_venv(tmp_path, monkeypatch)
    path = watchdog.write_hidden_launcher()
    body = path.read_text(encoding="utf-8")
    assert f'sh.Environment("PROCESS")("PYTHONPATH") = "{venv / "Lib" / "site-packages"}"' in body
    assert f'sh.CurrentDirectory = "{venv}"' in body
    runline = [ln for ln in body.splitlines() if ln.startswith("sh.Run")][0]
    assert str(base / "pythonw.exe") in runline
    assert str(venv) not in runline, "the venv stub exe must not appear in the action line"
    assert runline.rstrip().endswith(", 0, False")


def test_base_windowless_interpreter_needs_the_exe(tmp_path, monkeypatch):
    venv = tmp_path / "nv"
    (venv / "Lib" / "site-packages" / "galvanize").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = " + str(tmp_path / "gone") + "\n",
                                     encoding="utf-8")
    assert watchdog._base_windowless_interpreter(venv) is None
    monkeypatch.delenv("GALVANIZE_RUNTIME_VENV", raising=False)
    argv, env, cwd, free = watchdog.launcher_plan()
    assert not free, "a venv with no base pythonw must NOT claim console-free"


@NT_ONLY
def test_register_enables_a_disabled_stopgap_task(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog.winlaunch, "run",
                        lambda cmd, **kw: (calls.append(cmd), _Run(0))[1])
    monkeypatch.setattr(watchdog, "ensure_command",
                        lambda p=None: ["x.exe", "a"])
    ok, lines = watchdog.register()
    assert ok
    en = [c for c in calls
          if c[:2] == ["schtasks", "/Change"] and "/Enable" in c]
    assert en and en[0][3] == watchdog.task_name(), \
        "install must re-enable the task (stopgap /disable cannot survive)"
    assert "enabled" in lines[0]
