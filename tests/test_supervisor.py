"""Card C2 (audit F1): supervisor authority + READY protocol + liveness.

Acceptance map:
  mutex contention            -> test_mutex_contention_and_release,
                                 test_second_supervisor_exits_clean
  wedged-pre-main child       -> test_wedged_child_never_ready_is_killed_and_respawned
  respawn-after-kill          -> test_worker_kill_respawns_within_backoff
  capability-fail stop-retry  -> test_permanent_capability_failure_halts
  liveness contract           -> test_missed_ticks_trigger_tree_kill
  single owner / clean stop   -> test_graceful_stop_takes_worker_with_it
  primitive itself            -> test_monitored_primitive_membership_and_kill
  daemon.pid artifact gone    -> test_pid_file_artifact_removed
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from galvanize import supervisor as sup
from galvanize import winlaunch

FAKE = str(Path(__file__).resolve().parent / "_fake_worker.py")

FAST = dict(ready_timeout_s=4, tick_timeout_s=4, liveness_grace_s=2,
            backoff_base_s=0.3, backoff_cap_s=2.0, perm_fail_limit=2,
            poll_s=0.1, status_tick_s=0.5)


def wait_until(fn, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.15)
    return None


def snap(home):
    return sup.snapshot(home, max_age_s=120)


def borns(home):
    try:
        return (home / "borns.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def pid_alive(pid):
    # FIX-CI: powershell is not on POSIX runners; the assertion is pure
    # liveness semantics, so branch on the platform instead of the OS brand.
    if os.name != "nt":
        try:  # reap our own zombie; a reaped/zombie pid must read dead
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{'Y'}} else {{'N'}}"],
        capture_output=True, text=True).stdout.strip()
    return out == "Y"


class Harness:
    """Run the supervisor in-process against the fake worker; owns claim+thread."""

    def __init__(self, isolated_homes, monkeypatch, mode, **over):
        self.home = isolated_homes["galvanize"]
        monkeypatch.setenv("GALVANIZE_WORKER_CMD", f"{sys.executable} {FAKE} {mode}")
        self.cfg = sup.Config(**{**FAST, **over})
        self.claim = sup.claim_mutex(self.cfg.profile)
        self.sup = sup.Supervisor(self.cfg, self.claim, sup._setup_log(self.home),
                                  self.home)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sup.serve, args=(self.stop,),
                                       daemon=True)
        self.thread.start()

    def wait_state(self, *states, timeout=20.0):
        return wait_until(lambda: (lambda s: s if s and s.get("state") in states else None)(snap(self.home)),
                          timeout)

    def shutdown(self):
        self.stop.set()
        self.thread.join(15)
        assert not self.thread.is_alive(), "supervisor loop did not exit on stop"


# ------------------------------------------------------------------ authority

def test_mutex_contention_and_release(isolated_homes):
    if os.name != "nt":
        pytest.skip("cross-process named-mutex semantics; POSIX covered by "
                    "test_second_supervisor_exits_clean (flock path)")
    a = sup.claim_mutex("c2-contention")
    assert a.name.startswith("Global\\Galvanize-S-") and a.name.endswith("-c2-contention")
    with pytest.raises(sup.MutexHeld):
        sup.claim_mutex("c2-contention")
    a.release()
    b = sup.claim_mutex("c2-contention")  # reclaimable after release
    b.release()


def test_second_supervisor_exits_clean(isolated_homes):
    """Two supervisors => second exits clean, logs WHY (acceptance: by
    construction). The loser must not spawn a worker either."""
    claim = sup.claim_mutex(os.environ["GALVANIZE_PROFILE"])
    try:
        r = subprocess.run([sys.executable, "-m", "galvanize.cli", "supervise"],
                           capture_output=True, text=True, timeout=30,
                           cwd=str(Path(FAKE).parents[1]), env=dict(os.environ))
        assert r.returncode == 0, r.stderr
        log = (isolated_homes["galvanize"] / "supervisor.log").read_text(encoding="utf-8")
        assert "exiting clean" in log and "another live supervisor" in log
        assert not (isolated_homes["galvanize"] / "supervisor.json").exists()
        assert not (isolated_homes["galvanize"] / "ready.json").exists()
    finally:
        claim.release()


def test_pid_file_artifact_removed():
    """F14: daemon.pid is gone; the mutex is the authority."""
    from galvanize import paths
    assert not hasattr(paths, "pid_path")
    src = (Path(__file__).resolve().parents[1] / "galvanize" / "daemon.py"
           ).read_text(encoding="utf-8")
    assert "daemon.pid" not in src and "pid_path" not in src


# ------------------------------------------------------------------ primitive

def test_monitored_primitive_membership_and_kill():
    mon = winlaunch.spawn_monitored(
        [sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert mon.alive()
        assert mon.pid in mon.pids()
        assert mon.wait_exit(0.2) is None          # still running
        mon.kill_tree()
        assert mon.wait_exit(10) is not None       # job kill ends the tree
        assert not mon.alive()
    finally:
        mon.close()


# ------------------------------------------------------------------ readiness

def test_ready_accepted_and_torn_down_on_stop(isolated_homes, monkeypatch):
    h = Harness(isolated_homes, monkeypatch, "ticker")
    try:
        s = h.wait_state("ready")
        assert s, "supervisor never reached ready"
        w = s["worker"]
        assert w["pid"] and w["exe"] and w["version"] == "test"
        assert w["capabilities"] and "fake" in w["watchers"]
        assert s["mutex"]["name"].endswith(h.cfg.profile)
        assert s["state"] == "ready"
        worker_pid = w["pid"]
    finally:
        h.shutdown()
    assert snap(h.home) is None                    # clean teardown removes it
    assert not (h.home / "ready.json").exists()
    assert wait_until(lambda: not pid_alive(worker_pid), 15), \
        "worker survived supervisor teardown (job-kill failed)"


def test_wedged_child_never_ready_is_killed_and_respawned(isolated_homes, monkeypatch):
    """F1's 61864 wedge mode: process alive, silent, zero contribution.
    Simulated as a pre-main import sleep (importer that never posts READY)."""
    h = Harness(isolated_homes, monkeypatch, "wedged", ready_timeout_s=2)
    try:
        ok = wait_until(lambda: len(borns(h.home)) >= 2, 30)
        assert ok, f"no respawn after wedged child: {borns(h.home)}"
        s = snap(h.home)
        reasons = " | ".join(f["reason"] for f in s["failures"])
        assert "no READY" in reasons
        # the first (wedged) child must be DEAD, not orphaned: its pid is
        # gone from the world while the supervisor already respawned.
        wedged_pid = int(borns(h.home)[0].split()[0])
        assert wait_until(lambda: not pid_alive(wedged_pid), 15), \
            "wedged child orphaned by respawn (tree kill missing)"
    finally:
        h.shutdown()


def test_unauthenticated_ready_rejected(isolated_homes, monkeypatch):
    h = Harness(isolated_homes, monkeypatch, "bogus_token", ready_timeout_s=2)
    try:
        ok = wait_until(lambda: len(borns(h.home)) >= 2, 30)
        assert ok, "unauthenticated READY was accepted"
        s = snap(h.home)
        assert s["state"] != "ready"
    finally:
        h.shutdown()


def test_worker_kill_respawns_within_backoff(isolated_homes, monkeypatch):
    """Live-acceptance equivalent: kill the daemon process, the supervisor
    respawns it inside the capped backoff window."""
    h = Harness(isolated_homes, monkeypatch, "ticker")
    try:
        s = h.wait_state("ready")
        assert s
        victim = s["worker"]["pid"]
        t0 = time.time()
        os.kill(victim, getattr(signal, "SIGKILL", signal.SIGTERM))
        s2 = wait_until(lambda: (lambda x: x if x and x.get("worker")
                                 and x["worker"]["pid"] not in (None, victim)
                                 and x["state"] == "ready" else None)(snap(h.home)),
                        20)
        assert s2, "no respawn after worker kill"
        assert s2["spawn_count"] >= 2
        assert time.time() - t0 <= h.cfg.backoff_cap_s + 10
    finally:
        h.shutdown()


def test_missed_ticks_trigger_tree_kill(isolated_homes, monkeypatch):
    """READY came, main-loop progress stopped: liveness contract breach =>
    whole tree dies and we respawn."""
    h = Harness(isolated_homes, monkeypatch, "one_beat", liveness_grace_s=1.5)
    try:
        ok = wait_until(lambda: any("missed heartbeat" in f["reason"]
                                    for f in (snap(h.home) or {}).get("failures", [])), 30)
        assert ok, json.dumps((snap(h.home) or {}).get("failures", []))
        assert wait_until(lambda: len(borns(h.home)) >= 2, 20), \
            "no respawn after liveness breach"
    finally:
        h.shutdown()


def test_permanent_capability_failure_halts(isolated_homes, monkeypatch):
    """Judge: capability-permanent failure => stop retrying, surface loud."""
    h = Harness(isolated_homes, monkeypatch, "permfail")
    try:
        s = h.wait_state("halted", timeout=30)
        assert s, "supervisor never halted on permanent capability failure"
        assert "permanent capability failure" in s["halt_reason"]
        assert len(borns(h.home)) == h.cfg.perm_fail_limit
        log = (h.home / "supervisor.log").read_text(encoding="utf-8")
        assert "CRITICAL" in log and "HALTED" in log
        time.sleep(3 * h.cfg.backoff_cap_s)        # far beyond any backoff
        assert len(borns(h.home)) == h.cfg.perm_fail_limit, "respawned after halt"
    finally:
        h.shutdown()


def test_graceful_stop_takes_worker_with_it(isolated_homes, monkeypatch):
    h = Harness(isolated_homes, monkeypatch, "ticker")
    try:
        s = h.wait_state("ready")
        worker_pid = s["worker"]["pid"]
        # worker owns a heartbeat; stop must clear it (no lie left behind)
        sup.request_stop(h.home)
        assert sup.wait_stopped(20, h.home), "supervisor ignored the stop flag"
        assert wait_until(lambda: not pid_alive(worker_pid), 15)
        from galvanize import store as _st
        assert _st.get_heartbeat() is None, "heartbeat survived graceful stop"
    finally:
        h.stop.set()
        h.thread.join(15)


# ------------------------------------------------------------------ install / run

def test_snapshot_staleness_and_corpse(isolated_homes):
    """Fresh timestamp is not enough: a snapshot whose supervisor pid is
    dead (SIGKILL skips teardown) must read as absent, or racing installers
    adopt a corpse (live-drill finding)."""
    home = isolated_homes["galvanize"]
    (home / "supervisor.json").write_text(json.dumps(
        {"state": "ready", "updated": time.time(), "supervisor_pid": 999999,
         "worker": {"pid": 1}, "mutex": {"name": "x", "scope": "global"},
         "spawn_count": 1, "failures": []}), encoding="utf-8")
    assert sup.snapshot(home) is None                       # dead owner
    assert sup.raw_snapshot(home) is not None               # raw read still works
    (home / "supervisor.json").write_text(json.dumps(
        {"state": "ready", "updated": time.time() - 3600,
         "supervisor_pid": os.getpid(),
         "worker": {"pid": 1}, "mutex": {"name": "x", "scope": "global"},
         "spawn_count": 1, "failures": []}), encoding="utf-8")
    assert sup.snapshot(home) is None                       # stale tick
    assert sup.snapshot(home, max_age_s=7200)               # alive owner, young enough


def test_install_verifies_ready(tmp_path, monkeypatch):
    """wait_for_ready: consumes the supervisor snapshot the installer needs."""
    home = tmp_path / "g"
    home.mkdir()
    monkeypatch.setenv("GALVANIZE_HOME", str(home))
    assert sup.wait_for_ready(1.0, home) is None   # budget path: never appeared
    (home / "supervisor.json").write_text(json.dumps(
        {"state": "ready", "updated": time.time(), "supervisor_pid": os.getpid(),
         "worker": {"pid": 2}, "mutex": {"name": "x", "scope": "global"},
         "spawn_count": 1, "failures": []}), encoding="utf-8")
    s = sup.wait_for_ready(2.0, home)
    assert s and s["state"] == "ready"


def test_direct_run_refuses_when_authority_held(isolated_homes):
    """The double-start pathology by construction: a second `galvanize run`
    while ANY authority holder (supervisor or another unmanaged run) owns
    the mutex refuses loudly, immediately — no serve bind, no watcher build,
    no TOCTOU window."""
    env = dict(os.environ)
    claim = sup.claim_mutex(env["GALVANIZE_PROFILE"])
    try:
        r = subprocess.run([sys.executable, "-m", "galvanize.cli", "run"],
                           capture_output=True, text=True, timeout=60, env=env,
                           cwd=str(Path(FAKE).parents[1]))
        assert r.returncode == 0
        assert "already running" in r.stdout, r.stdout
        assert not (isolated_homes["galvanize"] / "supervisor.json").exists()
    finally:
        claim.release()


REPO = str(Path(FAKE).parents[1])


def _kill_tree(pid):
    # FIX-CI: taskkill is the Windows tool for the Job-Object tree-kill
    # drill; POSIX only needs the direct child to die (SIGKILL, the very
    # scenario the heartbeat-adoption test simulates).
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


def _dead_pid():
    """A pid that is DEAD BY FACT on every platform, not by range guess:
    spawn a no-op child and reap it. (999999 'looked' alive on macOS:
    out-of-range pids answer EPERM, not ESRCH, to signal 0.)"""
    q = subprocess.Popen([sys.executable, "-c", "pass"])
    q.wait()
    return q.pid


def test_worker_adopts_heartbeat_of_dead_predecessor(isolated_homes):
    """Drill defect (2026-10-21 21:22): a SIGKILLed worker left a beat that
    is fresh-by-age but dead-by-pid; the respawn must TAKE OVER, not stew
    in backoff for 60s. Refusal requires actual pid liveness."""
    home = isolated_homes["galvanize"]
    from galvanize import store as _st
    dead = _dead_pid()              # reaped child: dead on every OS
    _st.set_heartbeat(pid=dead)     # dead predecessor owns the beat ROW
    env = dict(os.environ, GALVANIZE_READY_TOKEN="adopt-probe",
               GALVANIZE_SUPERVISED="1")
    p = subprocess.Popen([sys.executable, "-m", "galvanize.cli", "run"],
                         env=env, cwd=REPO,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = wait_until(lambda: (sup.read_ready(home) or {}).get("token")
                        == "adopt-probe", 30)
        if not ok:
            # the child logged WHY (refused? crashed?) into its isolated
            # home; DEVNULL'd pipes must not mean flying blind on remote OSes
            try:
                why = (home / "galvanize.log").read_text(
                    encoding="utf-8", errors="replace")[-800:]
            except OSError:
                why = "(no daemon log)"
            assert False, ("supervised worker never posted READY over a dead "
                           f"predecessor's beat; child log tail:\n{why}")
        r = sup.read_ready(home)
        assert r["pid"] not in (dead,) and r["capabilities"]
    finally:
        _kill_tree(p.pid)


def test_worker_still_refuses_alive_legacy_daemon(isolated_homes):
    """The other side: a fresh beat from a process that IS alive (pre-C2
    orphan) must keep the refusal — the supervisor's loud respawns are the
    signal to stop it."""
    home = isolated_homes["galvanize"]
    from galvanize import store as _st
    _st.set_heartbeat(pid=os.getpid())   # fresh beat from a LIVE pid
    env = dict(os.environ, GALVANIZE_READY_TOKEN="refuse-probe",
               GALVANIZE_SUPERVISED="1")
    r = subprocess.run([sys.executable, "-m", "galvanize.cli", "run"],
                       env=env, cwd=REPO, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0
    assert "already running" in r.stdout
    assert not (home / "ready.json").exists()
