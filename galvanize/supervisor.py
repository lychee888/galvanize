"""Single-instance supervisor: SID-mutex authority + READY protocol (audit F1).

Judge-mandated design (card C2, verdict #1), stated as invariants:

1. THE MUTEX IS THE AUTHORITY. One supervisor process per (user SID, profile)
   claims `Global\\Galvanize-<SID>-<profile>` via CreateMutexW with an
   SID-restricted security descriptor BEFORE any heavy import or network
   touch. Workers never race: the supervisor is the only process that spawns
   them, and a second supervisor exits clean and logs why. O_EXCL pidfiles
   and heartbeat arbitration were REJECTED: a start-up-only liveness check
   is TOCTOU (audit F1 live proof: two daemons, same second, 20h apart in
   behavior). On a host where the standard user lacks the global-namespace
   right (ERROR_ACCESS_DENIED creating `Global\\`), the claim falls back to
   `Local\\` with the identical name suffix — within-session arbitration is
   what the pathology needs (both daemons run in the user's session), and
   the scope is recorded in supervisor.json so doctor tells the truth.
   POSIX gets the same role through an flock on supervisor.lock.
2. READINESS IS PROVEN, NOT ASSUMED. The worker is spawned through the C1
   primitive (suspended -> job -> resume, KILL_ON_JOB_CLOSE) and must post
   an authenticated READY (ready.json: per-spawn token injected in the
   child's env, pid matched against JOB MEMBERSHIP, interpreter identity,
   version, capabilities, per-watcher status) within GALVANIZE_READY_TIMEOUT_S
   (default 20). A pre-main wedge (loader hang, re-exec lost) times out the
   same way and the whole tree is job-killed.
3. LIVENESS IS A CONTRACT. After READY the worker must keep its main-loop
   heartbeat (state.json `_daemon`) fresh: first beat within
   GALVANIZE_TICK_TIMEOUT_S (30) and never older than GALVANIZE_LIVENESS_GRACE_S
   (45 ~= three missed 15s beats). Breach -> kill the whole tree, await job
   close, respawn with exponential backoff (GALVANIZE_BACKOFF_BASE_S=2, cap
   GALVANIZE_BACKOFF_CAP_S=300).
4. PERMANENT FAILURES HALT, NOT LOOP. A capability failure the worker marks
   permanent (e.g. imap-tools not importable while imap triggers are
   configured) cannot be fixed by respawn: after GALVANIZE_PERM_FAIL_LIMIT=2
   such READYs the supervisor stops retrying, surfaces CRITICAL in
   supervisor.log, and parks in state "halted" (tick continues; the C3
   watchdog can decide whether to relaunch the supervisor itself).
5. THE SUPERVISOR IS NOT SILENT. Its own rotating log (2 MB x 3, audit F10
   folded here) replaces the DEVNULL black hole; supervisor.json publishes
   state, mutex identity/scope, worker identity, spawn count, recent
   failures and a supervisor tick for readers (`daemon status`, doctor,
   `daemon install` verification, the C3 watchdog).

daemon.pid is GONE (F14): the mutex + supervisor.json supersede it.

Shutdown: `daemon stop` touches supervisor.shutdown; the supervisor polls
it, job-kills the worker, removes supervisor.json/ready.json, clears a
worker-owned heartbeat, exits 0. Force path: TerminateProcess on the
supervisor — its job handles close with it, and KILL_ON_JOB_CLOSE takes
the worker tree down anyway.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths as _paths
from . import winlaunch

SCHEMA_VERSION = 1


# ------------------------------------------------------------------ tunables

def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Config:
    def __init__(self, **kw):
        self.ready_timeout_s = kw.get("ready_timeout_s", _f("GALVANIZE_READY_TIMEOUT_S", 20))
        self.tick_timeout_s = kw.get("tick_timeout_s", _f("GALVANIZE_TICK_TIMEOUT_S", 30))
        self.liveness_grace_s = kw.get("liveness_grace_s", _f("GALVANIZE_LIVENESS_GRACE_S", 45))
        self.backoff_base_s = kw.get("backoff_base_s", _f("GALVANIZE_BACKOFF_BASE_S", 2))
        self.backoff_cap_s = kw.get("backoff_cap_s", _f("GALVANIZE_BACKOFF_CAP_S", 300))
        self.perm_fail_limit = kw.get("perm_fail_limit", _f("GALVANIZE_PERM_FAIL_LIMIT", 2))
        self.poll_s = kw.get("poll_s", _f("GALVANIZE_SUPERVISOR_POLL_S", 0.5))
        self.status_tick_s = kw.get("status_tick_s", _f("GALVANIZE_STATUS_TICK_S", 10))
        self.install_verify_s = kw.get("install_verify_s",
                                       _f("GALVANIZE_INSTALL_VERIFY_S",
                                          min(120.0, self.ready_timeout_s + 25)))
        # Test seam (also handy for exotic launch shapes): replaces the
        # default `[sys.executable, -m, galvanize.cli, run]` worker argv.
        # posix=False: Windows paths keep their backslashes.
        wcmd = os.environ.get("GALVANIZE_WORKER_CMD", "").strip()
        self.worker_cmd = shlex.split(wcmd, posix=False) if wcmd else None
        self.profile = os.environ.get("GALVANIZE_PROFILE", "").strip() or "default"

    def worker_argv(self) -> list[str]:
        return self.worker_cmd or [sys.executable, "-m", "galvanize.cli", "run"]


# ------------------------------------------------------------------ mutex

class MutexHeld(Exception):
    """Another live process owns the instance authority."""

    def __init__(self, name: str):
        super().__init__(f"instance mutex already held: {name}")
        self.name = name


class Claim:
    """Opaque handle proving we hold the authority; release() on shutdown."""

    def __init__(self, *, name: str, scope: str, handle=None, fd=None):
        self.name = name
        self.scope = scope  # "global" | "local" | "flock"
        self._handle = handle
        self._fd = fd

    def release(self):
        if self._handle is not None and os.name == "nt":
            _kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
        if self._fd is not None:
            try:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None


def mutex_name(profile: str = "default") -> str:
    return f"Global\\Galvanize-{_current_user_sid()}-{profile}"


def _as_claim_name(full_name: str) -> tuple[str, str]:
    """('Global\\X', scope) -> ('X', 'global'); Local\\ prefix similar."""
    if full_name.startswith("Global\\"):
        return full_name, "global"
    if full_name.startswith("Local\\"):
        return full_name, "local"
    return full_name, "flock"


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    _ERROR_ALREADY_EXISTS = 183
    _ERROR_ACCESS_DENIED = 5
    _TOKEN_QUERY = 0x0008
    _TokenUser = 1

    def _current_user_sid() -> str:
        tok = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(ctypes.c_void_p(-1), _TOKEN_QUERY,
                                          ctypes.byref(tok)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(tok, _TokenUser, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(tok, _TokenUser, buf, size,
                                                 ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
            sstr = wintypes.LPWSTR()
            if not _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(sstr)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return str(sstr.value)
            finally:
                _kernel32.LocalFree(sstr)
        finally:
            _kernel32.CloseHandle(tok)

    def _owner_sd():
        """SID-restricted security descriptor: only this user (and SYSTEM)
        may own/touch the mutex; other accounts get ERROR_ACCESS_DENIED,
        never silent cross-session interference."""
        sd = ctypes.c_void_p()
        size = wintypes.DWORD()
        sddl = "D:(A;;GA;;;%s)(A;;GA;;;SY)" % _current_user_sid()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(sd), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), sd, False)
        return sa, sd

    def claim_mutex(profile: str = "default") -> Claim:
        base = f"Galvanize-{_current_user_sid()}-{profile}"
        sa, sd = _owner_sd()
        try:
            for prefix, scope in (("Global\\", "global"), ("Local\\", "local")):
                name = prefix + base
                handle = _kernel32.CreateMutexW(ctypes.byref(sa), False, name)
                err = ctypes.get_last_error()
                if handle and err == _ERROR_ALREADY_EXISTS:
                    _kernel32.CloseHandle(handle)
                    raise MutexHeld(name)
                if handle:
                    return Claim(name=name, scope=scope, handle=int(handle))
                if err == _ERROR_ACCESS_DENIED and prefix == "Global\\":
                    continue  # no global-namespace right: fall back in-session
                raise ctypes.WinError(err)
        finally:
            _kernel32.LocalFree(sd)
        raise RuntimeError("unreachable: claim_mutex loop fell through")

else:  # POSIX: flock on supervisor.lock carries the same authority role.

    def _current_user_sid() -> str:  # pragma: no cover - posix hosts
        return str(os.getuid())

    def claim_mutex(profile: str = "default") -> Claim:
        import fcntl
        fd = os.open(_paths.claim_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise MutexHeld(f"supervisor.lock (profile {profile})") from None
        return Claim(name=f"galvanize-supervisor-{profile}", scope="flock", fd=fd)


# ------------------------------------------------------------------ files

def _write_json_atomic(path: Path, obj: dict) -> None:
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1)
        # Windows: a concurrent reader (status poll, C3 watchdog) can hold
        # the destination open without DELETE sharing for milliseconds;
        # os.replace then raises Access-denied. Retry briefly before giving
        # up — a lost tick is cosmetic, an exception in the supervisor loop
        # is not.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def snapshot(home: Path | None = None, max_age_s: float = 30.0) -> dict | None:
    """Live supervisor state, or None if absent/stale (a stale file means the
    supervisor is dead: hard-killed processes leave artifacts behind).
    A fresh-looking file whose supervisor pid no longer exists is equally
    dead — SIGKILL/TerminateProcess skips teardown, and a corpse within the
    tick window would otherwise advertise READY forever (drill finding:
    both racing installers adopted a killed supervisor's snapshot)."""
    st = _read_json((home or _paths.ensure_home()) / "supervisor.json")
    if not st:
        return None
    try:
        if time.time() - float(st.get("updated", 0)) > max_age_s:
            return None
        if not pid_alive(st.get("supervisor_pid", 0)):
            return None
    except (TypeError, ValueError):
        return None
    return st


def raw_snapshot(home: Path | None = None) -> dict | None:
    return _read_json((home or _paths.ensure_home()) / "supervisor.json")


def read_ready(home: Path | None = None) -> dict | None:
    return _read_json((home or _paths.ensure_home()) / "ready.json")


def request_stop(home: Path | None = None) -> None:
    (home or _paths.ensure_home()).mkdir(parents=True, exist_ok=True)
    (_paths.shutdown_flag_path() if home is None
     else home / "supervisor.shutdown").touch()


def wait_stopped(timeout_s: float = 15.0, home: Path | None = None) -> bool:
    """True once no live snapshot remains (graceful OR force both end here)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if snapshot(home) is None:
            return True
        time.sleep(0.25)
    return snapshot(home) is None


def wait_for_ready(timeout_s: float = 45.0, home: Path | None = None) -> dict | None:
    """`daemon install` verification hook (judge: the installer must confirm
    the supervisor posted READY, not fire-and-forget into DEVNULL). Polls
    until a terminal state (ready/halted) or the budget; returns the last
    snapshot (None if no supervisor ever surfaced)."""
    home = home or _paths.ensure_home()
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = snapshot(home)
        if last and last.get("state") in ("ready", "halted"):
            return last
        time.sleep(0.5)
    return last


def force_stop(snap: dict, home: Path | None = None) -> None:
    """TerminateProcess on Windows / SIGKILL on POSIX: the supervisor's job
    handles close with it, so the worker tree dies via KILL_ON_JOB_CLOSE."""
    pid = int(snap.get("supervisor_pid") or 0)
    if pid and pid != os.getpid():
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    h = home or _paths.ensure_home()
    (h / "supervisor.json").unlink(missing_ok=True)
    (h / "ready.json").unlink(missing_ok=True)
    (h / "supervisor.shutdown").unlink(missing_ok=True)


def log_tail(n_lines: int = 12, home: Path | None = None) -> list[str]:
    """Loud surface for `daemon install` failures (the DEVNULL era is over)."""
    try:
        text = (_paths.supervisor_log_path() if home is None
                else home / "supervisor.log").read_text(encoding="utf-8",
                                                        errors="replace")
        return text.strip().splitlines()[-n_lines:]
    except OSError:
        return []


# ------------------------------------------------------------------ worker side

def post_worker_ready(payload: dict) -> None:
    """Worker -> supervisor: authenticated READY (see module docstring §2).
    Called by galvanize.daemon once watchers are built and the first
    heartbeat is written; presence of GALVANIZE_READY_TOKEN gates it."""
    token = os.environ.get("GALVANIZE_READY_TOKEN", "")
    if not token:
        return
    home = _paths.ensure_home()
    _write_json_atomic(home / "ready.json", dict(payload, token=token, ts=time.time()))


# ------------------------------------------------------------------ supervisor

_SHUTDOWN = object()  # sentinel: graceful stop requested


class Supervisor:
    def __init__(self, cfg: Config, claim: Claim | None, log: logging.Logger,
                 home: Path | None = None):
        self.cfg = cfg
        self.claim = claim
        self.log = log
        self.home = home or _paths.ensure_home()
        self.spawn_count = 0
        self.perm_hits = 0
        self.consecutive_failures = 0
        self.failures: list[dict] = []
        self.worker_ready: dict | None = None
        self.mon = None
        self.halt_reason = ""
        self._state = "starting"

    # ---- status publishing -------------------------------------------------

    def _write_status(self, state: str | None = None):
        if state:
            self._state = state
        w = None
        if self.worker_ready and self._state in ("ready", "respawning", "stopping"):
            r = self.worker_ready
            w = {"pid": r.get("pid"), "exe": r.get("exe"), "version": r.get("version"),
                 "python": r.get("python"), "capabilities": r.get("capabilities"),
                 "watchers": r.get("watchers"), "ready_ts": r.get("ts")}
        _write_json_atomic(self.home / "supervisor.json", {
            "schema": SCHEMA_VERSION,
            "state": self._state,
            "supervisor_pid": os.getpid(),
            "supervisor_tick": time.time(),
            "updated": time.time(),
            "started": getattr(self, "_started", time.time()),
            "profile": self.cfg.profile,
            "mutex": {"name": self.claim.name if self.claim else None,
                      "scope": self.claim.scope if self.claim else None},
            "worker": w,
            "spawn_count": self.spawn_count,
            "consecutive_failures": self.consecutive_failures,
            "failures": self.failures[-8:],
            "halt_reason": self.halt_reason,
            "version": _self_version(),
        })

    # ---- helpers -------------------------------------------------------------

    def _shutdown_requested(self) -> bool:
        return (self.home / "supervisor.shutdown").exists()

    def _sleep(self, seconds: float, stop: threading.Event) -> bool:
        """Interruptible sleep; True if we should stop now."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if stop.is_set() or self._shutdown_requested():
                return True
            time.sleep(min(self.cfg.poll_s, max(0.01, deadline - time.monotonic())))
        return stop.is_set()

    def _read_beat(self):
        # C5: the heartbeat is a single UPSERT row in galvanize.db (was a
        # whole-file rmw of state.json racing the dispatcher threads).
        try:
            from . import store as _store
            hb = _store.get_heartbeat()
        except Exception:
            return None
        if not hb:
            return None
        try:
            return {"pid": int(hb["pid"]), "ts": float(hb["heartbeat"])}
        except (KeyError, TypeError, ValueError):
            return None

    def _spawn(self, token: str):
        env = dict(os.environ)
        env["GALVANIZE_SUPERVISED"] = "1"
        env["GALVANIZE_READY_TOKEN"] = token
        argv = self.cfg.worker_argv()
        self.log.info("worker spawn #%d: %s", self.spawn_count + 1, " ".join(argv))
        mon = winlaunch.spawn_monitored(argv, env=env, cwd=str(self.home))
        self.spawn_count += 1
        return mon

    def _read_ready(self, token: str, mon) -> dict | None:
        """Accept READY only if authenticated: our token AND a pid that is a
        member of OUR child's job (the venv pythonw stub can re-exec; the
        process that posts is the one in the job, not necessarily the pid
        CreateProcess handed us)."""
        r = read_ready(self.home)
        if not r or r.get("token") != token:
            return None
        try:
            pid = int(r.get("pid") or 0)
        except (TypeError, ValueError):
            return None
        if pid not in mon.pids():
            return None
        for field in ("exe", "version", "capabilities", "watchers"):
            if field not in r:
                return None
        return r

    def _fail(self, reason: str, mon, ready=None):
        self.failures.append({"ts": time.time(), "reason": reason[:300],
                              "spawn": self.spawn_count})
        self.consecutive_failures += 1
        self.worker_ready = ready
        self.log.warning("worker failure (%s): %s (respawn backoff pending)",
                         reason, f"spawn #{self.spawn_count}")
        try:
            if mon.alive():
                mon.kill_tree()
                mon.wait_exit(5)
        finally:
            mon.close()
        self.mon = None

    def _permanent_sig(self, ready: dict) -> list[str]:
        return sorted(n for n, w in (ready.get("watchers") or {}).items()
                      if isinstance(w, dict) and w.get("permanent"))

    # ---- the loop -------------------------------------------------------------

    def serve(self, stop: threading.Event) -> int:
        self._started = time.time()
        (self.home / "supervisor.shutdown").unlink(missing_ok=True)
        # Card C3: a supervisor that starts by any path (logon .lnk, manual,
        # watchdog) asserts intent-UP — the watchdog pause marker is the
        # `daemon stop` tombstone, consumed here.
        (self.home / "supervisor.pause").unlink(missing_ok=True)
        (self.home / "ready.json").unlink(missing_ok=True)
        self._write_status("starting")
        try:
            while not stop.is_set():
                outcome = self._attempt(stop)
                if outcome == _SHUTDOWN:
                    return 0
                # failure path: backoff, then respawn (unless perm-hits demand halt)
                delay = min(self.cfg.backoff_base_s *
                            (2 ** max(self.consecutive_failures - 1, 0)),
                            self.cfg.backoff_cap_s)
                self._write_status("respawning")
                self.log.info("respawn in %.1fs (failure #%d)",
                              delay, self.consecutive_failures)
                if self._sleep(delay, stop):
                    return 0
                if (self.perm_hits and
                        self.perm_hits >= self.cfg.perm_fail_limit):
                    self.halt_reason = (f"permanent capability failure(s) observed "
                                        f"{self.perm_hits}x: "
                                        f"{self._permanent_sig(self.worker_ready or {})}")
                    return self.serve_halt(stop)
        finally:
            self.teardown()
        return 0

    def serve_halt(self, stop: threading.Event) -> int:
        self._write_status("halted")
        self.log.critical("HALTED: %s — not respawning; fix the environment, "
                          "then restart the supervisor (fixes permanently-failing "
                          "capabilities only by re-install, not by retry)",
                          self.halt_reason)
        while not stop.is_set():
            if self._shutdown_requested():
                return 0
            self._write_status()
            if self._sleep(max(self.cfg.status_tick_s, 1.0), stop):
                return 0
        return 0

    def _attempt(self, stop: threading.Event):
        """One spawn -> READY -> liveness-contract cycle. Returns _SHUTDOWN,
        'halt', or None (failed; caller respawns with backoff)."""
        cfg = self.cfg
        token = secrets.token_hex(16)
        try:
            (self.home / "ready.json").unlink(missing_ok=True)
            mon = self._spawn(token)
        except OSError as e:
            self.failures.append({"ts": time.time(),
                                  "reason": f"spawn failed: {e}"[:300],
                                  "spawn": self.spawn_count})
            self.consecutive_failures += 1
            self.log.error("worker spawn failed: %s", e)
            return None
        self.mon = mon
        self._write_status()

        # 1) authenticated READY within ready_timeout
        deadline = time.monotonic() + cfg.ready_timeout_s
        ready = None
        while time.monotonic() < deadline:
            if stop.is_set() or self._shutdown_requested():
                self._fail("shutdown before READY", mon)
                return _SHUTDOWN
            ready = self._read_ready(token, mon)
            if ready:
                break
            rc = mon.wait_exit(cfg.poll_s)
            if rc is not None:
                self._fail(f"worker exited rc={rc} before posting READY", mon)
                return None
        if not ready:
            self._fail(f"no READY within {cfg.ready_timeout_s:.0f}s "
                       "(wedged pre-main or unauthenticated)", mon)
            return None

        # 2) READY accepted: interpreter identity + per-watcher status recorded
        perm = self._permanent_sig(ready)
        if perm:
            self.perm_hits += 1
            self.log.warning("READY reports permanent capability failure(s): %s "
                             "(hit %d/%d)", ", ".join(perm), self.perm_hits,
                             cfg.perm_fail_limit)
        else:
            self.perm_hits = 0
        self.worker_ready = ready
        self.log.info("READY accepted: worker pid %s exe %s v%s (spawns %d)",
                      ready.get("pid"), ready.get("exe"), ready.get("version"),
                      self.spawn_count)
        self._write_status("ready")

        # 3) liveness contract: heartbeat from a job member, first within
        #    tick_timeout of spawn, thereafter never older than liveness_grace.
        spawn_at = time.monotonic()
        got_beat = False
        last_publish = 0.0
        while True:
            if stop.is_set() or self._shutdown_requested():
                self._write_status("stopping")
                mon.kill_tree()
                mon.wait_exit(5)
                mon.close()
                self.mon = None
                return _SHUTDOWN
            rc = mon.wait_exit(cfg.poll_s)
            if rc is not None:
                self._fail(f"worker exited rc={rc}", mon, ready=ready)
                return None
            beat = self._read_beat()
            fresh = (beat and beat["pid"] in mon.pids()
                     and time.time() - beat["ts"] <= cfg.liveness_grace_s)
            if fresh:
                if not got_beat:
                    got_beat = True
                    self.consecutive_failures = 0
                    self.log.info("main-loop heartbeat confirmed (pid %s)",
                                  beat["pid"])
            if not got_beat and time.monotonic() - spawn_at > cfg.tick_timeout_s:
                self._fail(f"no main-loop heartbeat within "
                           f"{cfg.tick_timeout_s:.0f}s of spawn", mon, ready=ready)
                return None
            if got_beat and beat and time.time() - beat["ts"] > cfg.liveness_grace_s:
                self._fail(f"missed heartbeat ticks (age "
                           f"{time.time() - beat['ts']:.0f}s)", mon, ready=ready)
                return None
            now = time.monotonic()
            if now - last_publish >= cfg.status_tick_s:
                last_publish = now
                self._write_status()

    def teardown(self):
        mon, self.mon = self.mon, None
        if mon is not None:
            try:
                mon.kill_tree()
                mon.wait_exit(5)
            finally:
                mon.close()
        # the worker is gone; if it owned the heartbeat, don't leave a lie
        beat = self._read_beat()
        if beat and self.worker_ready and beat["pid"] == self.worker_ready.get("pid"):
            try:
                from . import state as _state
                _state.clear_heartbeat()
            except Exception:
                pass
        for f in ("supervisor.json", "ready.json", "supervisor.shutdown"):
            try:
                (self.home / f).unlink(missing_ok=True)
            except OSError:
                pass
        if self.claim is not None:
            self.claim.release()


def _self_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "?"


def _pid_alive_nt(pid: int) -> bool:
    """Windows-safe liveness probe (os.kill on Windows TERMINATES, it does
    not probe): OpenProcess + exit-code peek."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # ERROR_ACCESS_DENIED (5) => the process EXISTS, we just cannot
        # query it: treat as alive. ERROR_INVALID_PARAMETER (87) => reaped.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def pid_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_nt(pid)
    try:  # POSIX sig 0 probes without killing
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _setup_log(home: Path) -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("galvanize.supervisor")
    log.setLevel(logging.INFO)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
    fh = RotatingFileHandler(str(home / "supervisor.log"), maxBytes=2_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if os.environ.get("GALVANIZE_SUPERVISE_VERBOSE"):
        log.addHandler(logging.StreamHandler())
    return log


def main(argv=None) -> int:
    """`galvanize supervise`: claim authority BEFORE heavy imports, then serve.
    Exits 0 cleanly (with a logged reason) when another supervisor owns the
    mutex — the double-daemon pathology becomes impossible by construction."""
    cfg = Config()
    home = _paths.ensure_home()
    try:
        claim = claim_mutex(cfg.profile)
    except MutexHeld as held:
        # Loser path: one honest line into the shared supervisor.log, no
        # rotating handler (avoid two writers churning rotation on boot).
        msg = (time.strftime("%Y-%m-%d %H:%M:%S") +
               f" INFO galvanize.supervisor: exiting clean: another live "
               f"supervisor already owns the instance authority ({held.name})\n")
        try:
            with open(home / "supervisor.log", "a", encoding="utf-8") as fh:
                fh.write(msg)
        except OSError:
            pass
        print(f"  supervisor already owns the mutex ({held.name}); exiting.")
        return 0
    log = _setup_log(home)
    log.info("supervisor start: pid=%d profile=%s mutex=%s scope=%s",
             os.getpid(), cfg.profile, claim.name, claim.scope)
    # Card C5: the first supervisor start owns the one-time migration of
    # the pre-C5 JSON state + triggers.yaml first-seed into galvanize.db.
    # A failure here must NOT take the supervisor down (the worker and the
    # doctor surface it loudly); it must not be silently swallowed either.
    try:
        from . import store as _store
        _m = _store.ensure_ready()
        if not _m.get("skipped"):
            log.info("state migration into galvanize.db: %s", _m)
    except Exception as e:
        log.error("state store migration failed (worker will retry; doctor "
                  "will surface it): %s: %s", type(e).__name__, e)
    sup = Supervisor(cfg, claim, log, home)
    stop = threading.Event()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: stop.set())
            except ValueError:
                pass
    try:
        return sup.serve(stop)
    finally:
        log.info("supervisor stop: pid=%d spawns=%d", os.getpid(), sup.spawn_count)


if __name__ == "__main__":
    sys.exit(main())
