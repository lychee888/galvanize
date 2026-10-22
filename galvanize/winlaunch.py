"""Windows launch primitive — the ONLY place galvanize spawns processes.

Audit 2026-10-21 F5/F6/F12/F7 + judge verdict #4 + live Win11 evidence
(2026-10-21 evening, operator note). Every call site in the package goes
through :func:`run` (managed child) or :func:`spawn_detached` (daemon
handoff); the AST gate in ``tests/test_launch_primitive_ast.py`` enforces
it (allowlist entries carry their justification).

FLAG POLICY — CREATE_NO_WINDOW is the DEFAULT flavor, DETACHED_PROCESS the
exception. Live proof on this box (Windows 11 + Windows Terminal as the
default terminal): spawning the venv 'pythonw.exe' stub DETACHED allocated
a VISIBLE Windows Terminal window for the stub's console-subsystem re-exec
child (the base python.exe it re-invokes); with CREATE_NO_WINDOW the child
owns a hidden console and every descendant — re-execs included — inherits
it, so nothing ever draws. DETACHED_PROCESS means "no console now, take a
fresh one the moment you touch a console API" — under WT that fresh
console HAS a window. DETACHED is only correct for a child that genuinely
wants a console attached later; galvanize has none.

Two shapes:

* :func:`run` — a child whose lifetime we manage and whose output we
  capture. Created CREATE_SUSPENDED, assigned to a private Job Object
  with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, then resumed: the child cannot
  run (and cannot fork out grandchildren) before job membership is
  established. Plus CREATE_NO_WINDOW: without it a windowless daemon
  spawning console children (cmd.exe, node, claude, codex, git, schtasks,
  powershell) gets a fresh VISIBLE console per wake — audit F5, the
  2026-10-10 schtasks-flash class. Timeouts call TerminateJobObject: the
  WHOLE TREE dies, not just the direct child — which is what kills the
  cmd.exe-orphan-keeps-the-window case (audit F12). A descendant that
  outlives the direct child is likewise killed when run() closes the job.

* :func:`spawn_detached` — the daemon handoff (``daemon install`` starting
  the run child). It must OUTLIVE the spawning process, so it is
  deliberately NOT in a kill-on-close job. Flags: CREATE_NO_WINDOW |
  CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB — hidden-console
  flavor (above), new group so console signals can't reach it, breakaway
  so the harness session's own job cannot take the daemon down with it.
  If the parent job forbids breakaway the call retries without it and
  reports ``escaped_job=False``: the caller must surface that, never
  swallow it.

CAVEAT 1 (judge): invisibility covers CONSOLE windows only. A shell wake
executes user-declared commands; any GUI descendant (notepad, a message
box, an Electron app) can still draw a window. Nothing this primitive does
can prevent a command from being chatty.

CAVEAT 2 (operator; ties into cards C2/C4): a file NAMED pythonw.exe is
not guaranteed windowless — venv stubs re-exec the base console
interpreter, exactly why the hidden-console flavor is mandatory. C4 probes
the launched IMAGE, not the filename; C2's READY verification must fail an
install whose child came up with a visible console allocation.

POSIX: run() uses start_new_session=True + killpg on timeout — process
group as tree, same semantics, different mechanism. Output is decoded
text (utf-8, errors='replace') on both platforms; TimeoutExpired carries
the partial output collected before the kill.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from subprocess import CompletedProcess

# Process creation flags (Windows constant values; harmless literals on POSIX)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000

# Popen objects for handoff children: GC'ing a still-running Popen emits
# ResourceWarning noise (and Python 3.11's Popen has no detach()).
_HANDOFFS: list = []


def run(cmd, *, timeout=None, env=None, cwd=None, shell=False) -> CompletedProcess:
    """Managed child: invisible, captured, tree-killed on timeout.

    ``cmd`` is a str (with shell=True: the exact command line handed to
    cmd.exe /c on Windows, /bin/sh -c elsewhere) or an argv list. Returns a
    CompletedProcess whose stdout/stderr are ALWAYS str (utf-8, errors=
    replace). Raises subprocess.TimeoutExpired (with .output/.stderr text)
    after killing the entire process tree, and OSError when the executable
    cannot be created.
    """
    if os.name == "nt":
        return _win_run(cmd, timeout=timeout, env=env, cwd=cwd, shell=shell)
    return _posix_run(cmd, timeout=timeout, env=env, cwd=cwd, shell=shell)


def spawn_monitored(cmd, *, env=None, cwd=None) -> "Monitored":
    """Supervisor worker child: created suspended -> job -> resumed, managed.

    Same creation discipline as run() (no pre-membership window; the venv
    stub and any re-exec grandchild are inside the job from their first
    instruction) but WITHOUT waiting: the returned Monitored object keeps
    the handles alive so the supervisor can poll exit status (wait_exit),
    enumerate job membership (pids), and take the WHOLE TREE down on a
    liveness breach (kill_tree -> TerminateJobObject + close). Callers
    MUST eventually close(); a GC'd still-running child mirrors
    spawn_detached's handoff noise.
    """
    if os.name == "nt":
        return _win_spawn_monitored(cmd, env=env, cwd=cwd)
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, cwd=cwd, start_new_session=True)
    return _PosixMonitored(p)


def spawn_detached(cmd, *, env=None, cwd=None) -> tuple[int, bool]:
    """Fire-and-forget handoff child that must outlive us: no job, hidden console.

    Returns (pid, escaped_job). escaped_job=False means the spawn happened
    WITHOUT BREAKAWAY_FROM_JOB (the parent job forbids it) and the child
    will die with this session — callers must surface that, never swallow it.
    """
    if os.name != "nt":
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env, cwd=cwd,
                             start_new_session=True)
        _HANDOFFS.append(p)
        return p.pid, True
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB

    def _popen(creationflags):
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env, cwd=cwd,
                             creationflags=creationflags)
        _HANDOFFS.append(p)
        return p.pid

    try:
        return _popen(flags), True
    except OSError:
        # Parent job disallows breakaway: start session-tied and let the
        # caller say so out loud (audit F14: this mask verified correct).
        return _popen(flags & ~CREATE_BREAKAWAY_FROM_JOB), False


# ---------------------------------------------------------------- POSIX

def _posix_run(cmd, timeout, env, cwd, shell):
    p = subprocess.Popen(cmd, shell=shell, env=env, cwd=cwd,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # start_new_session: p.pid == pgid
        except (ProcessLookupError, AttributeError):
            pass
        out, err = p.communicate()
        raise subprocess.TimeoutExpired(
            cmd, timeout, output=_text(out), stderr=_text(err)) from None
    return CompletedProcess(cmd, p.returncode, _text(out), _text(err))


def _text(b):
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return b or ""


class _PosixMonitored:
    """POSIX twin of the Windows Monitored handle: one process group = tree."""

    def __init__(self, popen):
        self._p = popen
        self.pid = popen.pid

    def alive(self) -> bool:
        return self._p.poll() is None

    def wait_exit(self, timeout_s):
        try:
            return self._p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None

    def kill_tree(self):
        try:
            os.killpg(self._p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def pids(self):
        return {self.pid}

    def close(self):
        try:
            self._p.__exit__(None, None, None)  # closes pipes; child already reaped/killed
        except Exception:
            pass


# ---------------------------------------------------------------- Windows

if os.name == "nt":
    import _winapi
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # Explicit prototypes: without restype/argtypes ctypes truncates HANDLE
    # returns/args to 32-bit on x64 and every handle operation misfires
    # (observed live: an access violation from a truncated env pointer, and
    # ERROR_INVALID_PARAMETER for every raw CreateProcessW env block).
    # Process creation therefore goes through _winapi — the exact stdlib
    # path, which owns the env-block encoding — while job/wait plumbing
    # stays raw.
    _K32 = _kernel32
    _K32.CreatePipe.restype = wintypes.BOOL
    _K32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE),
                                ctypes.POINTER(wintypes.HANDLE),
                                ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD]
    _K32.CreateFileW.restype = wintypes.HANDLE
    _K32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD,
                                 wintypes.DWORD, wintypes.HANDLE]
    _K32.SetHandleInformation.restype = wintypes.BOOL
    _K32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    _K32.CreateJobObjectW.restype = wintypes.HANDLE
    _K32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _K32.SetInformationJobObject.restype = wintypes.BOOL
    _K32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD]
    _K32.AssignProcessToJobObject.restype = wintypes.BOOL
    _K32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _K32.ResumeThread.restype = wintypes.DWORD
    _K32.ResumeThread.argtypes = [wintypes.HANDLE]
    _K32.TerminateJobObject.restype = wintypes.BOOL
    _K32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _K32.WaitForSingleObject.restype = wintypes.DWORD
    _K32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _K32.GetExitCodeProcess.restype = wintypes.BOOL
    _K32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _K32.QueryInformationJobObject.restype = wintypes.BOOL
    _K32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
    _K32.CloseHandle.restype = wintypes.BOOL
    _K32.CloseHandle.argtypes = [wintypes.HANDLE]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9
    _INFINITE = 0xFFFFFFFF
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_FAILED = 0xFFFFFFFF
    _GENERIC_RW = 0xC0000000  # NUL device rejects GENERIC_ALL (returns -1)
    _INVALID_HANDLES = (None, 0, -1, 0xFFFFFFFFFFFFFFFF)
    _FILE_SHARE_RW = 0x00000003
    _OPEN_EXISTING = 3
    _HANDLE_FLAG_INHERIT = 0x00000001

    def _wtext(b):
        return b.decode("utf-8", errors="replace") if b else ""

    def _pump(handle, chunks):
        # Windows os.read() speaks CRT fds, not raw HANDLEs: bind the pipe
        # handle to an fd first (os.close then owns and closes the handle).
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except OSError:
            _K32.CloseHandle(handle)
            return
        try:
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError:  # ERROR_BROKEN_PIPE == EOF for us
                    break
                if not data:
                    break
                chunks.append(data)
        finally:
            os.close(fd)  # also CloseHandle()s the underlying handle

    def _win_run(cmd, timeout, env, cwd, shell):
        if shell:
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", cmd]
        else:
            argv = [cmd] if isinstance(cmd, str) else list(cmd)
        cmdline = subprocess.list2cmdline(argv)
        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)

        def pipe():
            r, w = wintypes.HANDLE(), wintypes.HANDLE()
            if not _K32.CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 0):
                raise ctypes.WinError(ctypes.get_last_error())
            # Only the WRITE ends may be inherited: a child that copied our
            # read end into a grandchild would deadlock EOF detection.
            _K32.SetHandleInformation(r, _HANDLE_FLAG_INHERIT, 0)
            return r.value, w.value

        r_out, w_out = pipe()
        r_err, w_err = pipe()
        nul = _K32.CreateFileW("NUL", _GENERIC_RW, _FILE_SHARE_RW,
                               ctypes.byref(sa), _OPEN_EXISTING, 0, None)
        if nul in _INVALID_HANDLES:
            raise ctypes.WinError(ctypes.get_last_error())

        si = subprocess.STARTUPINFO(
            dwFlags=_winapi.STARTF_USESTDHANDLES,
            hStdInput=nul, hStdOutput=w_out, hStdError=w_err)
        # subprocess semantics: env keys uppercased, values coerced to str.
        env_map = None if env is None else {str(k).upper(): str(v) for k, v in env.items()}
        try:
            h_proc, h_thread, _pid, _tid = _winapi.CreateProcess(
                None, cmdline, None, None, True,
                CREATE_SUSPENDED | CREATE_NO_WINDOW,
                env_map, cwd, si)
        except OSError:
            for h in (w_out, w_err, nul, r_out, r_err):
                _K32.CloseHandle(h)
            raise
        for h in (w_out, w_err, nul):  # child has its own copies now
            _K32.CloseHandle(h)
        w_out = w_err = nul = None

        h_job = None
        out_chunks, err_chunks = [], []
        try:
            h_job = _K32.CreateJobObjectW(None, None)
            if not h_job:
                raise ctypes.WinError(ctypes.get_last_error())
            lim = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            lim.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _K32.SetInformationJobObject(
                    h_job, _JobObjectExtendedLimitInformation,
                    ctypes.byref(lim), ctypes.sizeof(lim)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not _K32.AssignProcessToJobObject(h_job, h_proc):
                # Fail loudly: an unassigned child would defeat tree-kill.
                raise ctypes.WinError(ctypes.get_last_error())
            if _K32.ResumeThread(h_thread) == _WAIT_FAILED:  # (DWORD)-1 == failure
                raise ctypes.WinError(ctypes.get_last_error())
            _K32.CloseHandle(h_thread)
            h_thread = None

            readers = [threading.Thread(target=_pump, args=(r_out, out_chunks), daemon=True),
                       threading.Thread(target=_pump, args=(r_err, err_chunks), daemon=True)]
            for t in readers:
                t.start()
            r_out = r_err = None  # owned by the reader threads (os.close)

            ms = _INFINITE if timeout is None else min(int(float(timeout) * 1000), _INFINITE - 1)
            wait = _K32.WaitForSingleObject(h_proc, ms)
            if wait == _WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            if wait == _WAIT_TIMEOUT:
                _K32.TerminateJobObject(h_job, 124)  # kills the WHOLE tree
                _K32.WaitForSingleObject(h_proc, 5000)
            # Closing the job kills any descendant still inside it
            # (KILL_ON_JOB_CLOSE); then every pipe write end is gone and
            # the readers finish.
            _K32.CloseHandle(h_job)
            h_job = None
            for t in readers:
                t.join(timeout=5)
            rc = wintypes.DWORD()
            _K32.GetExitCodeProcess(h_proc, ctypes.byref(rc))
            _K32.CloseHandle(h_proc)
            h_proc = None
            out, err = _wtext(b"".join(out_chunks)), _wtext(b"".join(err_chunks))
            if wait == _WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
            return CompletedProcess(cmd, rc.value, out, err)
        finally:
            for h in (h_thread, h_job, h_proc, r_out, r_err, w_out, w_err, nul):
                if h:
                    _K32.CloseHandle(h)


    class _WinMonitored:
        """Live handles to a job-member child: poll, enumerate, tree-kill, close."""

        _STILL_ACTIVE = 259
        _PROCESS_QUERY_LIMITED = 0x1000

        def __init__(self, h_proc, h_thread, h_job, pid):
            self._h_proc, self._h_thread, self._h_job = h_proc, h_thread, h_job
            self.pid = pid

        def alive(self) -> bool:
            if not self._h_proc:
                return False
            rc = wintypes.DWORD()
            if not _K32.GetExitCodeProcess(self._h_proc, ctypes.byref(rc)):
                return False
            return rc.value == self._STILL_ACTIVE

        def wait_exit(self, timeout_s):
            """Return the exit code if the child exited within timeout, else None."""
            if not self._h_proc:
                return None
            ms = min(int(float(timeout_s) * 1000), _INFINITE - 1)
            if _K32.WaitForSingleObject(self._h_proc, ms) != 0:  # WAIT_OBJECT_0
                return None
            rc = wintypes.DWORD()
            if not _K32.GetExitCodeProcess(self._h_proc, ctypes.byref(rc)):
                return -1
            return rc.value

        def kill_tree(self):
            """TerminateJobObject: every job member (stub, re-exec grandchild,
            any descendant) dies. Then closing the job handle drops our last
            reference so KILL_ON_JOB_CLOSE reaps late joiners too."""
            if self._h_job:
                _K32.TerminateJobObject(self._h_job, 1)

        def pids(self) -> set:
            """Current job membership. The heartbeat/READY pid check runs
            against THIS set, not just the spawn pid: the venv pythonw stub
            may re-exec the base interpreter, and the process that actually
            serves the protocol is the child, not the pid we were handed."""
            if not self._h_job:
                return set()
            # JOBOBJECT_BASIC_PROCESS_ID_LIST {u32 All, u32 ThisArray,
            #   uptr Unique, uptr Others[...]}: 4 KiB covers absurd fan-out.
            buf = ctypes.create_string_buffer(4096)
            ret = wintypes.DWORD()
            if not _K32.QueryInformationJobObject(
                    self._h_job, 3, buf, ctypes.sizeof(buf), ctypes.byref(ret)):
                return {self.pid}
            n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[1]  # ThisArray count
            pids = ctypes.cast(ctypes.byref(buf, 8),
                               ctypes.POINTER(ctypes.c_size_t * max(int(n), 1)))
            return {int(p) for p in pids.contents}

        def close(self):
            for h in (self._h_thread, self._h_job, self._h_proc):
                if h:
                    _K32.CloseHandle(h)
            self._h_thread = self._h_job = self._h_proc = None

    def _win_spawn_monitored(cmd, env, cwd):
        argv = [cmd] if isinstance(cmd, str) else list(cmd)
        cmdline = subprocess.list2cmdline(argv)
        env_map = None if env is None else {str(k).upper(): str(v) for k, v in env.items()}
        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        nuls = []
        for _ in range(3):
            h = _K32.CreateFileW("NUL", _GENERIC_RW, _FILE_SHARE_RW,
                                 ctypes.byref(sa), _OPEN_EXISTING, 0, None)
            if h in _INVALID_HANDLES:
                raise ctypes.WinError(ctypes.get_last_error())
            nuls.append(h)
        si = subprocess.STARTUPINFO(dwFlags=_winapi.STARTF_USESTDHANDLES,
                                    hStdInput=nuls[0], hStdOutput=nuls[1],
                                    hStdError=nuls[2])
        # Hidden-console flavor (module docstring policy); suspended until the
        # job with KILL_ON_JOB_CLOSE is attached — no unmonitored run window.
        h_proc, h_thread, pid, _tid = _winapi.CreateProcess(
            None, cmdline, None, None, True,
            CREATE_SUSPENDED | CREATE_NO_WINDOW, env_map, cwd, si)
        for h in nuls:
            _K32.CloseHandle(h)
        h_job = _K32.CreateJobObjectW(None, None)
        if not h_job:
            _K32.CloseHandle(h_proc)
            _K32.CloseHandle(h_thread)
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            lim = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            lim.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _K32.SetInformationJobObject(
                    h_job, _JobObjectExtendedLimitInformation,
                    ctypes.byref(lim), ctypes.sizeof(lim)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not _K32.AssignProcessToJobObject(h_job, h_proc):
                raise ctypes.WinError(ctypes.get_last_error())
            if _K32.ResumeThread(h_thread) == _WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            _K32.TerminateJobObject(h_job, 1)
            _K32.CloseHandle(h_job)
            _K32.CloseHandle(h_proc)
            _K32.CloseHandle(h_thread)
            raise
        return _WinMonitored(h_proc, h_thread, h_job, pid)

    Monitored = _WinMonitored
else:
    Monitored = _PosixMonitored
