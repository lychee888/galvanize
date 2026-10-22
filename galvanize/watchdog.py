"""External watchdog: a minute-cadence scheduled `daemon ensure` (card C3, audit F2).

The C2 supervisor self-heals worker crashes WHILE it is alive and the box
is logged in; nothing recovered from (a) a supervisor that dies or wedges
between logon and now, or (b) a logon where the Startup .lnk never fired.
This module registers a per-user Task Scheduler entry that runs
`galvanize daemon ensure` every minute; ensure is a cheap, idempotent
"should a supervisor be running? then make sure one is" pass — the
judge's external-watchdog point: a wedged process cannot watchdog itself.

FEASIBILITY GATE (empirical, 2026-10-21, this host, un-elevated interactive
token — Medium IL with BUILTIN\\Administrators "deny only"):
  - schtasks /Create /SC MINUTE /MO 1 ................ OK (probe
    GalvanizeProbe-C3/-c: registered, fired every minute, Last Result 0,
    435 appended ticks in ~5.5 h; Logon Mode "Interactive only").
  - BUT the probe's console-subsystem action (cmd.exe /c ...) demonstrably
    popped a VISIBLE Windows Terminal window on every trigger (verifier
    sample 22:08-22:12: exactly 60.0 s cadence, 5/5 CASCADIA hits).
    => the shipped task cannot point at python/cmd directly; it runs a
    generated wscript hidden-launcher (window style 0, proven pattern on
    this box: .galvanize/run_hidden.vbs) — windowless BY CONSTRUCTION.
  - schtasks /Create ... /RU <self> with NO /RP ...... OK (probe -d:
    accepted; stays "Interactive only" — no stored credential => no S4U,
    so a minute task CANNOT run pre-logon on this host).
  - schtasks /Create /SC ONLOGON ..................... Access denied
    (reproduces the historical F2 finding; hence the minute-cadence task,
    not an ONLOGON trigger).
  - PowerShell Set-ScheduledTask on our own task ..... OK un-elevated
    (used to flip DisallowStartIfOnBatteries, which schtasks /Create sets
    to true by default and which would stall the watchdog on a laptop).

Guarantee actually delivered, stated where users see it (doctor/status):
post-LOGON persistence within <= 60 s of any logon plus wedge recovery
while running; PRE-logon (headless boot) is NOT available on this host
without a stored password. That is strictly stronger than the .lnk alone,
which is kept for instant start at logon (belt) while this task is the
guarantee (braces) — if the .lnk or its shortcut ever fails, coverage
degrades to <= 60 s, not to silence.
"""

from __future__ import annotations

import getpass
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths as _paths
from . import winlaunch

TASK_PREFIX = "GalvanizeWatchdog"

# A live supervisor whose supervisor.json stopped being rewritten: the
# longest LEGITIMATE silent window is the respawn backoff cap (300 s)
# between the single "respawning" publish and the next status write
# (supervisor.serve loop); + ready timeout + margin. Below this we treat a
# stale-but-alive supervisor as "backing off" and do nothing.
WEDGE_AFTER_S = 420.0


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def wedge_after_s() -> float:
    return _f("GALVANIZE_WATCHDOG_WEDGE_S", WEDGE_AFTER_S)


def resolve_profile(profile: str | None = None) -> str:
    return (profile or os.environ.get("GALVANIZE_PROFILE", "").strip()
            or "default")


def task_name(profile: str | None = None) -> str:
    p = resolve_profile(profile)
    return TASK_PREFIX if p == "default" else f"{TASK_PREFIX}-{p}"


# ------------------------------------------------------------------ spec

def ensure_command(profile: str | None = None) -> list[str]:
    """Windowless interpreter argv running `daemon ensure` (same exe
    resolution as autostart._galvanize_run_cmd: venv pythonw when present;
    the hidden-console policy that keeps the stub re-exec windowless is
    winlaunch's, and the scheduler honors CREATE_NO_WINDOW per the probe).
    """
    from . import autostart
    cmd = autostart._galvanize_run_cmd()          # [exe, -m, galvanize.cli, supervise]
    return cmd[:-1] + ["daemon", "ensure", "--profile", resolve_profile(profile)]


def tr_string(cmd: list[str]) -> str:
    """schtasks /TR value: native quoting (quoted exe + bare args)."""
    return subprocess.list2cmdline(cmd)


def _base_windowless_interpreter(venv: Path) -> Path | None:
    """The GUI-subsystem interpreter UNDERLYING a venv, or None.

    Why (CoS field note, card C6): the venv's pythonw.exe is a launcher
    STUB that re-execs the base CPython; the re-exec'd child allocates a
    Windows Terminal pseudoconsole that outlives a short-lived process,
    so every minute-tick leaked one zombie WT frame (19 in ~20 min on the
    live box). The base install's pythonw.exe is a real GUI-subsystem
    exe: no stub, no re-exec, no console, at any process lifetime. We
    read it straight from the venv's pyvenv.cfg `home =` line (the same
    file `python -m venv` writes), NOT by globbing an interpreter vendor.
    """
    if os.name != "nt":
        return None
    cfg = venv / "pyvenv.cfg"
    try:
        for ln in cfg.read_text(encoding="utf-8").splitlines():
            k, _, v = ln.partition("=")
            if k.strip().lower() == "home":
                base = Path(v.strip()) / "pythonw.exe"
                return base if base.exists() else None
    except OSError:
        pass
    return None


def launcher_plan(profile: str | None = None) -> tuple[list[str], dict[str, str], str, bool]:
    """(argv, env, cwd, console_free) for the scheduled `daemon ensure`.

    Preferred (console_free=True): run the BASE interpreter's pythonw
    with PYTHONPATH pointing at the app venv's site-packages — imports
    the identical galvanize install the stub would have re-exec'd into,
    but with no re-exec and therefore no console allocation at all.
    Fallback (any platform/layout we cannot resolve): today's recorded
    venv stub, which winlaunch keeps hidden-but-brittle (see
    _base_windowless_interpreter for the leak).
    """
    from . import runtime
    home = _paths.ensure_home()
    venv = runtime.venv_dir()
    base = _base_windowless_interpreter(venv)
    site = venv / "Lib" / "site-packages"
    if base is not None and site.is_dir() and (site / "galvanize").is_dir():
        argv = [str(base), "-s", "-m", "galvanize.cli",
                "daemon", "ensure", "--profile", resolve_profile(profile)]
        return argv, {"PYTHONPATH": str(site)}, str(venv), True
    return (ensure_command(profile), {"PYTHONPATH": ""}, str(home), False)


def hidden_launcher_path(profile: str | None = None) -> Path:
    p = resolve_profile(profile)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in p)
    return _paths.galvanize_home() / (
        "watchdog_hidden.vbs" if safe == "default" else f"watchdog_hidden_{safe}.vbs")


def write_hidden_launcher(profile: str | None = None) -> Path:
    """The windowless action wrapper (operator field note, verifier sample
    2026-10-21 22:08-22:12; CoS re-verification card C6): a CONSOLE-
    subsystem /TR payload (cmd.exe, and plain python.exe) provably pops a
    visible Windows Terminal window on this box every trigger, and a
    venv-STUB pythonw is not enough either — the stub re-execs the base
    console interpreter and the pseudoconsole outlives the short tick
    process (19 zombie WT frames in ~20 min, field census). So the entry
    point here is double-belt: wscript.exe (GUI subsystem, window style
    0) launching whatever launcher_plan says is console-free — on this
    layout, the BASE install's pythonw.exe with PYTHONPATH into the app
    venv, no re-exec anywhere in the chain.
    The wait=false also lets wscript exit promptly, so the scheduler's
    task instance ends while our ensure pass keeps running detached.
    HERMES_HOME stays scrubbed (same hygiene as the legacy launcher:
    guards the uv-interpreter inheritance caveat).
    """
    argv, env, cwd, _free = launcher_plan(profile)
    cmd = tr_string(argv)
    lines = [
        "' galvanize watchdog hidden launcher — generated by "
        "`galvanize daemon install`, do not edit",
        'Set sh = CreateObject("WScript.Shell")',
        f'sh.CurrentDirectory = "{cwd}"',
        'sh.Environment("PROCESS")("HERMES_HOME") = ""',
    ]
    for k, v in env.items():
        lines.append(f'sh.Environment("PROCESS")("{k}") = "{v}"')
    lines.append(f'sh.Run "{cmd.replace(chr(34), chr(34) * 2)}", 0, False')
    body = "\n".join(lines) + "\n"
    path = hidden_launcher_path(profile)
    path.write_text(body, encoding="utf-8")
    return path


def schtasks_spec(profile: str | None = None, *, user: str | None = None) -> list[str]:
    """argv for registration (pure; unit-tested). The /TR is the wscript
    trampoline, NOT the interpreter: windowless by construction (see
    write_hidden_launcher). /RU <self> without /RP: accepted on this host
    and deterministic; /F makes install idempotent."""
    tr = tr_string(["wscript.exe", str(hidden_launcher_path(profile))])
    return ["schtasks", "/Create", "/F",
            "/TN", task_name(profile),
            "/TR", tr,
            "/SC", "MINUTE", "/MO", "1",
            "/RU", user or getpass.getuser()]


def _battery_spec(name: str) -> list[str]:
    ps = ('$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries '
          '-DontStopIfGoingOnBatteries; '
          f'Set-ScheduledTask -TaskName \'{name}\' -Settings $s | Out-Null')
    return ["powershell", "-NoProfile", "-Command", ps]


# ------------------------------------------------------------------ register

def register(profile: str | None = None) -> tuple[bool, list[str]]:
    """Create/refresh the minute-cadence task. Never raises; the caller
    surfaces failure as the EXPLICIT weaker guarantee (card: no silent
    downgrade). Card C6 adds the /Enable belt: an operator stopgap
    (`schtasks /change /disable`) must not survive a fresh install."""
    if os.name != "nt":
        return True, ["watchdog: not applicable off Windows "
                      "(systemd Restart=on-failure / launchd KeepAlive own that role)."]
    name = task_name(profile)
    try:
        write_hidden_launcher(profile)
        r = winlaunch.run(schtasks_spec(profile))
    except OSError as e:
        return False, [f"✖ watchdog task registration failed: {e}"]
    if r.returncode != 0:
        why = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f"rc={r.returncode}"
        return False, [f"✖ watchdog task registration failed: {why}",
                       "  persistence: logon-only on this host (watchdog task not registered)"]
    # CoS stopgap retirement (card C6): re-enabling an enabled task is a
    # no-op success; this guarantees a /disable'd watchdog comes back UP
    # on any `daemon install` without asking the operator to remember it.
    winlaunch.run(["schtasks", "/Change", "/TN", name, "/Enable"])
    # battery gate: schtasks defaults Stop/DisallowStartIfOnBatteries=true;
    # un-elevated Set-ScheduledTask on our own task verified OK on this box.
    b = winlaunch.run(_battery_spec(name))
    entry = ("console-free entry (base pythonw, no stub re-exec)"
             if launcher_plan(profile)[3] else
             "venv-stub entry (re-exec may leak a WT window per tick)")
    lines = [f"✔ Watchdog task '{name}' registered+enabled: `daemon ensure` "
             f"every minute while logged in, {entry} "
             f"(post-logon guarantee; pre-logon needs stored credentials)."]
    if b.returncode != 0:
        lines.append("  ⚠ could not clear the battery start-gate (task may not run on battery).")
    return True, lines


def unregister(profile: str | None = None) -> tuple[bool, list[str]]:
    if os.name != "nt":
        return True, ["watchdog: nothing to remove off Windows."]
    name = task_name(profile)
    winlaunch.run(["schtasks", "/End", "/TN", name])
    r = winlaunch.run(["schtasks", "/Delete", "/F", "/TN", name])
    hidden_launcher_path(profile).unlink(missing_ok=True)   # zero-residue (operator note)
    if r.returncode == 0:
        return True, [f"✔ Watchdog task '{name}' removed."]
    out = (r.stderr or r.stdout).lower()
    if "cannot find" in out or "not found" in out or "系统找不到" in out:
        return True, [f"watchdog task '{name}' was not registered."]
    return False, [f"✖ watchdog task removal failed: {(r.stderr or r.stdout).strip()[:200]}"]


_QUERY_CACHE: dict[str, tuple[float, bool]] = {}


def registered(profile: str | None = None, *, ttl_s: float = 60.0) -> bool:
    """Query with a small TTL cache: status()/doctor call this and the
    scheduler query costs ~100 ms."""
    if os.name != "nt":
        return False
    name = task_name(profile)
    now = time.time()
    hit = _QUERY_CACHE.get(name)
    if hit and now - hit[0] < ttl_s:
        return hit[1]
    ok = winlaunch.run(["schtasks", "/Query", "/TN", name], timeout=15).returncode == 0
    _QUERY_CACHE[name] = (now, ok)
    return ok


# ------------------------------------------------------------------ ensure

def pause() -> None:
    _paths.ensure_home().mkdir(parents=True, exist_ok=True)
    _paths.pause_flag_path().touch()


def resume() -> None:
    _paths.pause_flag_path().unlink(missing_ok=True)


def paused() -> bool:
    return _paths.pause_flag_path().exists()


def _log() -> logging.Logger:
    lg = logging.getLogger("galvanize.watchdog")
    if not lg.handlers:
        _paths.ensure_home()
        h = RotatingFileHandler(str(_paths.watchdog_log_path()),
                                maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg


def ensure(profile: str | None = None, *,
           snapshot=None, raw=None, force=None, spawn=None) -> dict:
    """One idempotent maintenance pass (what the scheduled task runs).

    Decisions (all seams injectable for tests):
      paused / shutdown-flag ....... action=paused   (user stopped it: leave down)
      fresh snapshot ............... action=noop     (healthy supervisor)
      live pid, tick stale ......... action=backing-off (within wedge window)
                                       action=restarted   (older: WEDGED -> kill tree + respawn)
      no snapshot / corpse pid ..... action=started

    Spawning goes through winlaunch.spawn_detached (the C1 primitive):
    hidden console, breakaway so the child outlives this task instance.
    A second concurrent ensure can race us; the SID mutex claim makes the
    loser exit clean, so overlap is harmless by construction (C2).
    """
    from . import supervisor as sup
    snap = (snapshot if snapshot is not None else sup.snapshot)()
    rawsnap = (raw if raw is not None else sup.raw_snapshot)()
    alive = (sup.pid_alive(rawsnap.get("supervisor_pid", 0))
             if rawsnap else False)
    result = {"ts": time.time(), "profile": resolve_profile(profile)}

    def _action(act: str, detail: str) -> dict:
        result.update(action=act, detail=detail)
        if act != "noop":
            _log().info("ensure: %s — %s", act, detail)
        return result

    # user-stopped intent beats everything: flags are the tombstone
    if paused() or (rawsnap is None and
                    _paths.shutdown_flag_path().exists()):
        return _action("paused", "watchdog pause marker present (daemon stop); not resurrecting")
    if snap is not None:
        return _action("noop", f"supervisor pid {snap.get('supervisor_pid')} "
                               f"state {snap.get('state')}, tick fresh")

    do_force = force or sup.force_stop

    def _default_spawn():
        from . import autostart
        return winlaunch.spawn_detached(autostart._win_startup_cmd())

    do_spawn = spawn or _default_spawn

    if rawsnap is not None and alive:
        stale = time.time() - float(rawsnap.get("updated") or 0)
        if stale <= wedge_after_s():
            return _action("backing-off", f"supervisor pid {rawsnap.get('supervisor_pid')} "
                               f"alive, status {stale:.0f}s old (<= {wedge_after_s():.0f}s "
                               f"legit respawn-backoff window)")
        do_force(rawsnap)
        pid, escaped = do_spawn()
        detail = (f"killed wedged supervisor pid {rawsnap.get('supervisor_pid')} "
                  f"(state {rawsnap.get('state')}, tick {stale:.0f}s old)"
                  + ("" if escaped else "; WARNING: child bound to this session's job — "
                                          "will not outlive the task instance"))
        return _action("restarted", detail + f"; spawned supervisor pid {pid}")

    # nothing alive at all (down, or corpse file from a hard kill)
    if rawsnap is not None:
        do_force(rawsnap)   # clear artifacts so the new supervisor has no corpse to adopt
    try:
        pid, escaped = do_spawn()
    except OSError as e:
        return _action("failed", f"spawn failed: {e}")
    detail = (f"no live supervisor (snapshot {'corpse' if rawsnap else 'absent'}); "
              f"spawned pid {pid}" + ("" if escaped else " ⚠ session-bound (will die with the task)"))
    return _action("started", detail)


def persistence_summary(profile: str | None = None) -> dict:
    """What guarantees this host actually has, for doctor/status — the
    card's 'never silently downgrade' surface."""
    from . import autostart
    win = os.name == "nt"
    reg = registered(profile) if win else False
    lnk = autostart.installed()
    return {
        "watchdog_task": reg,
        "watchdog_applicable": win,
        "startup_lnk": lnk,
        "scope": ("post-logon (<= 60 s of any logon; pre-logon needs stored "
                  "credentials this host does not keep)" if win else
                  "session-managed (systemd/launchd)"),
        "note": ("watchdog + .lnk" if reg and lnk else
                 "watchdog task only (.lnk missing; start latency <= 60 s)" if reg else
                 "logon-only on this host (watchdog task not registered)" if lnk else
                 "NONE — run: galvanize daemon install"),
    }
