"""Start-at-login install for the galvanize daemon.

User-facing behavior (PLAN §5): after `galvanize init`, the daemon is
registered to start at login and running right now. Mechanisms per OS:
  Windows : Task Scheduler, per-user onlogon task (no admin, hidden window)
  Linux   : systemd user unit (+ enable --now; lingering hint if no session)
  macOS   : LaunchAgent plist + launchctl bootstrap

Everything is per-user, reversible (`daemon remove`), and prints exactly
what it did. The task/agent runs `galvanize run`; hot state lives in
~/.galvanize so a reboot resumes all triggers.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .paths import ensure_home


TASK_NAME = "GalvanizeTriggerDaemon"
UNIT_NAME = "galvanize"


def _galvanize_run_cmd() -> list[str]:
    """Interpreter + args that run `galvanize run` with no console window.

    Handles both launch shapes: `python -m galvanize` (sys.executable is the
    interpreter) and the installed `galvanize.exe` launcher (find python(.w)
    next to it). Prefers pythonw.exe on Windows — windowless.
    """
    exe = Path(sys.executable)
    if exe.name.lower() not in ("python.exe", "pythonw.exe"):
        # frozen entry-point launcher -> sibling interpreter in Scripts/..
        cand = exe.parent.parent / "Scripts" / "python.exe"
        if not cand.exists():
            cand = exe.parent / "python.exe"
        if cand.exists():
            exe = cand
    if os.name == "nt":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            exe = windowless
    return [str(exe), "-m", "galvanize.cli", "run"]


# ------------------------------------------------------------------ Windows

def _startup_lnk() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Galvanize.lnk"


def _write_startup_shortcut() -> None:
    """Shell:startup .lnk via PowerShell WScript.Shell (no admin needed)."""
    target, *arglist = _galvanize_run_cmd()
    args = " ".join(arglist)
    lnk = _startup_lnk()
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath='%s';$s.Arguments='%s';$s.WorkingDirectory='%s';$s.WindowStyle=7;$s.Save()"
        % (str(lnk).replace("'", "''"), target, args,
           str(ensure_home()).replace("'", "''"))
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, text=True, check=True)


def _win_install() -> tuple[bool, list[str]]:
    # Preferred: Startup-folder shortcut — per-user, no elevation.
    try:
        _write_startup_shortcut()
        lines = ["✔ Daemon will start at login (Startup shortcut created)."]
        from . import state as _state
        if _state.live_daemon_pid():
            lines.append(f"✔ Daemon already running (pid {_state.live_daemon_pid()}) — not starting a second.")
            return True, lines
        # start it right now, detached, windowless. BREAKAWAY_FROM_JOB matters:
        # without it a daemon spawned from a harness session inherits that
        # session's kill-on-close Job Object and dies with the session
        # (observed: repeated "the daemon died" reports, no 'daemon stopped'
        # log line = hard-killed, never the SIGTERM path).
        creationflags = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED | NEW_GROUP | BREAKAWAY
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      stdin=subprocess.DEVNULL)
        try:
            subprocess.Popen(_win_startup_cmd(), creationflags=creationflags, **kwargs)
        except OSError:
            # Parent job forbids breakaway (Job_OBJECT_MSG / nested jobs):
            # still start, just session-tied, and say so.
            subprocess.Popen(_win_startup_cmd(),
                             creationflags=creationflags & ~0x01000000, **kwargs)
            lines.append("  ⚠ started inside this session's process job (will not outlive it);"
                         " log back in or run the Startup shortcut for a detached daemon.")
        lines.append("✔ Daemon started now.")
        return True, lines
    except Exception as e:
        return False, [f"✖ Startup shortcut failed: {e}",
                       "  Fallback: galvanize run in a terminal, or 'galvanize daemon install' as admin"]


def _win_startup_cmd() -> list[str]:
    """Direct detached run (pythonw -m galvanize.cli run)."""
    return _galvanize_run_cmd()


def _win_remove() -> tuple[bool, list[str]]:
    ok = True
    lines = []
    lnk = _startup_lnk()
    try:
        lnk.unlink(missing_ok=True)
        lines.append("✔ Removed Startup shortcut (won't start at login).")
    except OSError as e:
        ok = False
        lines.append(f"✖ Could not remove Startup shortcut: {e}")
    # also clear a legacy schtasks entry if one exists
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
    return ok, lines


def _win_installed() -> bool:
    return _startup_lnk().exists()


# ------------------------------------------------------------------ Linux

def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{UNIT_NAME}.service"


def _linux_install() -> tuple[bool, list[str]]:
    lines = []
    exe = " ".join(_galvanize_run_cmd())
    unit = (
        "[Unit]\nDescription=Galvanize trigger daemon\n\n"
        "[Service]\nExecStart=" + exe + "\nRestart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    _unit_path().parent.mkdir(parents=True, exist_ok=True)
    _unit_path().write_text(unit, encoding="utf-8")
    p = subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT_NAME}.service"],
                       capture_output=True, text=True)
    if p.returncode == 0:
        return True, ["✔ systemd user service enabled and started.",
                      "  (Server/logind headless: run 'loginctl enable-linger $USER' to keep it up at logout.)"]
    return False, [f"✖ systemctl failed: {(p.stderr or p.stdout).strip()[:200]}",
                   f"  Unit written to {_unit_path()} — enable manually."]


def _linux_remove() -> tuple[bool, list[str]]:
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT_NAME}.service"],
                   capture_output=True)
    try:
        _unit_path().unlink(missing_ok=True)
    except OSError:
        pass
    return True, ["✔ systemd user service removed."]


def _linux_installed() -> bool:
    return _unit_path().exists()


# ------------------------------------------------------------------ macOS

def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"agent.galvanize.{UNIT_NAME}.plist"


def _macos_install() -> tuple[bool, list[str]]:
    plist = {
        "Label": f"agent.galvanize.{UNIT_NAME}",
        "ProgramArguments": _galvanize_run_cmd(),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
    }
    _plist_path().parent.mkdir(parents=True, exist_ok=True)
    with open(_plist_path(), "wb") as fh:
        plistlib.dump(plist, fh)
    p = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(_plist_path())],
                       capture_output=True, text=True)
    if p.returncode == 0 or "already loaded" in (p.stderr or "").lower():
        return True, ["✔ LaunchAgent installed and loaded."]
    return False, [f"✖ launchctl failed: {(p.stderr or p.stdout).strip()[:200]}"]


def _macos_remove() -> tuple[bool, list[str]]:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(_plist_path())],
                   capture_output=True)
    try:
        _plist_path().unlink(missing_ok=True)
    except OSError:
        pass
    return True, ["✔ LaunchAgent removed."]


def _macos_installed() -> bool:
    return _plist_path().exists()


# ------------------------------------------------------------------ facade

def _os() -> str:
    if sys.platform == "win32":
        return "win"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def install() -> tuple[bool, list[str]]:
    ensure_home()
    return {"win": _win_install, "linux": _linux_install, "macos": _macos_install}[_os()]()


def remove() -> tuple[bool, list[str]]:
    return {"win": _win_remove, "linux": _linux_remove, "macos": _macos_remove}[_os()]()


def installed() -> bool:
    return {"win": _win_installed, "linux": _linux_installed, "macos": _macos_installed}[_os()]()
