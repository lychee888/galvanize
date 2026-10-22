"""Application-owned runtime + the launch-equivalent capability gate (card C4).

Audit F3 root cause, restated: `daemon install` used to adopt the INVOKER's
interpreter into the persistence entries, so running install from any other
python (uv shim, repo dir, a woken agent's own env) silently retargeted the
.lnk to an interpreter that might not be able to import the watcher deps at
all — and nothing noticed until the daemon failed silently for 20 h. Judge
verdict #2: an import probe is necessary but INSUFFICIENT; what we persist
must be a validated CAPABILITY MANIFEST, and a failed validation must
PRESERVE the previous working installation, never retarget quietly.

The rule this module enforces:

  1. `daemon install` provisions/refreshes an APP-OWNED venv at
     ~/.galvanize/runtime/venv (base: `python -m venv --upgrade-deps`, then
     `pip install` of this same source with the [imap] extra), and probes
     it launch-equivalently. That interpreter is what every persistence
     entry (.lnk, watchdog vbs, systemd unit, LaunchAgent) points at.
  2. The INVOKER env is only a FALLBACK, and only after passing the same
     probe — and install says so out loud.
  3. The probe is launch-equivalent, not in-process wishful thinking:
     candidate interpreter launched with `-I` (isolated: no inherited
     PYTHONPATH, no user-site, no env smuggles), argv equivalent to what
     the .lnk runs, at the .lnk's working directory, emitting the shared
     capability manifest (galvanize.capability, schema v1). Dependency /
     auth / env / version failures are distinguished; windowless-ness of
     the FINAL target is checked (audit F4).
  4. The chosen identity + full probe result are recorded in
     ~/.galvanize/install.json (MANIFEST_SCHEMA 1). Doctor renders the
     three-way diff: manifest vs persistence target vs live daemon
     identity (READY capabilities ride the same schema via C2).
  5. Probe/provision failure -> LOUD refusal, exit 1, and NOTHING on disk
     touched: previous install.json, .lnk and watchdog task stay exactly
     as they were (the "preserve" half of the card).

Test/manual seams (all opt-in env vars, documented in README):
  GALVANIZE_RUNTIME_VENV           override the app venv location
  GALVANIZE_RUNTIME_DISABLE=1      consider no app venv at all
  GALVANIZE_RUNTIME_SKIP_PROVISION=1  never run venv/pip; validate only
  GALVANIZE_INSTALL_INTERP=<exe>   override the invoker-fallback candidate
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import paths as _paths
from . import winlaunch
from .capability import PROBE_SCHEMA

MANIFEST_SCHEMA = 1
MANIFEST_NAME = "install.json"

# Everything that can make the probe child import DIFFERENT code than the
# daemon launched by the .lnk will see (the uv/PYTHONPATH disease from F3).
SCRUB_ENV_KEYS = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "PYTHONWARNINGS",
    "GALVANIZE_READY_TOKEN", "GALVANIZE_SUPERVISED", "GALVANIZE_WORKER_CMD",
)


def _nt() -> bool:
    return os.name == "nt"


# ------------------------------------------------------------------ paths

def runtime_dir() -> Path:
    return _paths.galvanize_home() / "runtime"


def venv_dir() -> Path:
    override = os.environ.get("GALVANIZE_RUNTIME_VENV", "").strip()
    if override:
        return Path(override).expanduser()
    return runtime_dir() / "venv"


def manifest_path() -> Path:
    return _paths.galvanize_home() / MANIFEST_NAME


def venv_python(venv: Path | None = None, *, windowless: bool = True) -> Path | None:
    """The interpreter inside a venv that a persistence entry may point at.

    On Windows the target must be pythonw.exe (F4); if the venv has no
    windowless launcher we return the console exe only when the caller
    asked for it (the probe then FAILS the windowless capability and the
    gate refuses, out loud — we do not quietly accept a console target).
    """
    v = venv_dir() if venv is None else venv
    scripts = "Scripts" if _nt() else "bin"
    names = (("pythonw.exe", "python.exe") if windowless else ("python.exe",)) \
        if _nt() else ("python", "python3")
    for n in names:
        p = v / scripts / n
        if p.exists():
            return p
    return None


# ------------------------------------------------------------------ probe

def probe_env(extra: dict | None = None) -> dict:
    """Env for the probe child: inherited minus anything that can shadow
    imports, plus explicit overrides (card: WITHOUT inherited PYTHONPATH)."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in SCRUB_ENV_KEYS}
    env.update(extra or {})
    return env


def probe_command(exe: str | Path, *, isolated: bool = True) -> list[str]:
    return [str(exe)] + (["-I"] if isolated else []) + \
        ["-m", "galvanize.capability", "--json"]


def _launch_failure(exe: str | Path, detail: str) -> dict:
    return {"schema": PROBE_SCHEMA, "ts": time.time(), "exe": str(exe),
            "python": "?", "version": "?", "venv_prefix": None, "isolated": True,
            "capabilities": {}, "versions": {},
            "keyring": {"backend": None, "roundtrip": False},
            "failures": [{"check": "launch", "kind": "env", "detail": detail[:400]}],
            "warnings": [], "ok": False}


def probe(exe: str | Path, *, isolated: bool = True, extra_env: dict | None = None,
          cwd: Path | str | None = None, timeout: float = 60.0) -> dict:
    """Launch-equivalent capability probe of a candidate interpreter.

    Returns the schema-v1 manifest the child printed; a child that cannot
    launch, times out, or emits garbage comes back as an ok=False manifest
    with kind "env" so callers render one uniform structure.
    """
    cmd = probe_command(exe, isolated=isolated)
    env = (probe_env(extra_env) if isolated
           else dict(os.environ, **(extra_env or {})))
    try:
        r = winlaunch.run(cmd, timeout=timeout, env=env,
                          cwd=str(cwd or _paths.ensure_home()))
    except OSError as e:
        return _launch_failure(exe, f"cannot exec {exe}: {e}")
    out = (r.stdout or "").strip()
    manifest = None
    if out:
        try:
            manifest = json.loads(out)
        except ValueError:
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        manifest = json.loads(line)
                        break
                    except ValueError:
                        continue
    if not isinstance(manifest, dict) or manifest.get("schema") != PROBE_SCHEMA:
        tail = ((r.stderr or r.stdout or "").strip().splitlines() or [f"rc={r.returncode}"])[-1]
        return _launch_failure(exe, f"probe emitted no v{PROBE_SCHEMA} manifest "
                                    f"(rc={r.returncode}): {tail[:300]}")
    return manifest


def describe_probe(m: dict, *, who: str) -> list[str]:
    """Installer-facing lines with failure kinds named (the card's
    dependency-vs-network/auth distinction)."""
    out = [f"✖ probe of {who} ({m.get('exe')}):"]
    for f in m.get("failures") or []:
        kind = f.get("kind", "?")
        label = {"dependency": "missing/broken dependency",
                 "auth": "keyring backend (auth)",
                 "env": "environment/launch shape",
                 "version": "version mismatch"}.get(kind, kind)
        out.append(f"  ✖ [{label}] {f.get('check')}: {f.get('detail')}")
    for w in m.get("warnings") or []:
        out.append(f"  ⚠ {w}")
    return out


# ------------------------------------------------------------------ manifest

def load_manifest(home: Path | None = None) -> dict | None:
    try:
        m = json.loads((manifest_path() if home is None
                        else home / MANIFEST_NAME).read_text(encoding="utf-8"))
        return m if isinstance(m, dict) and m.get("schema") == MANIFEST_SCHEMA else None
    except (OSError, ValueError):
        return None


def write_manifest(m: dict, home: Path | None = None) -> Path:
    from .supervisor import _write_json_atomic
    p = manifest_path() if home is None else home / MANIFEST_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(p, m)
    return p


def manifest_launch_cmd(home: Path | None = None) -> list[str] | None:
    """The recorded, probed launch argv — the ONLY thing persistence may
    use once C4 has installed. None if no (or broken) manifest."""
    m = load_manifest(home)
    if not m:
        return None
    cmd = ((m.get("launch") or {}).get("cmd")) or []
    if not cmd or not Path(str(cmd[0])).exists():
        return None
    return [str(x) for x in cmd]


# ------------------------------------------------------------------ provisioning

def _install_specs() -> list[str]:
    """What pip installs into the app venv, with the [imap] extra spelled
    out (card wording). Dev checkout -> editable against this tree, so the
    persisted venv keeps running working-tree code (same contract C2/C3 had
    via the repo .venv); released installs pin the published wheel."""
    src = Path(__file__).resolve().parents[1]
    if (src / "pyproject.toml").exists() and (src / "galvanize" / "__init__.py").exists():
        return ["-e", f"{src}[imap]"]
    try:
        from importlib.metadata import distribution
        raw = distribution("galvanize").read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            url = info.get("url", "")
            if url.startswith(("http://", "https://", "git+")):
                vcs = (info.get("vcs_info") or {}).get("commit_id", "")
                if not url.startswith("git+"):
                    url = f"git+{url}"
                if vcs:
                    url = f"{url}@{vcs}"
                return [f"galvanize[imap] @ {url}"]
    except Exception:
        pass
    from . import __version__
    return [f"galvanize[imap]=={__version__}"]


_NETWORK_HINTS = ("could not fetch", "connection", "timed out", "timeout",
                  "name or service", "proxy", "ssl", "certificate",
                  "networkconfiguration", "unable to retrieve", "read timeout",
                  "max retries")


def _classify_pip_failure(text: str) -> str:
    low = (text or "").lower()
    return "network" if any(h in low for h in _NETWORK_HINTS) else "dependency"


def provision(venv: Path | None = None, *, refresh: bool = False) -> tuple[bool, str, list[str]]:
    """Create (or refresh) the app-owned venv. Returns
    (ok, fail_kind, lines); never raises. `refresh` re-runs pip only
    (version-drift repair); otherwise full `python -m venv --upgrade-deps`."""
    v = venv_dir() if venv is None else venv
    lines: list[str] = []
    verb = "Refreshing" if refresh else "Provisioning"
    lines.append(f"{verb} app-owned runtime at {v} ...")
    env = probe_env()
    if not refresh:
        base = sys.executable
        try:
            r = winlaunch.run([base, "-m", "venv", "--upgrade-deps", str(v)],
                              timeout=300, env=env)
        except OSError as e:
            return False, "env", lines + [f"  ✖ cannot run venv on {base}: {e}"]
        if r.returncode != 0:
            tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["?"])[-1]
            return False, _classify_pip_failure(tail), lines + [
                f"  ✖ python -m venv failed (rc={r.returncode}): {tail[:240]}",
                "    the base interpreter may lack ensurepip (some uv/embedded "
                "pythons); install a full CPython and re-run daemon install."]
    exe = venv_python(v, windowless=False)
    if exe is None:
        return False, "env", lines + [f"  ✖ venv at {v} has no interpreter"]
    from . import __version__ as _ver
    cmd = [str(exe), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    if refresh:
        cmd.append("--upgrade")
    cmd += _install_specs()
    try:
        r = winlaunch.run(cmd, timeout=900, env=env)
    except OSError as e:
        return False, "env", lines + [f"  ✖ cannot run pip in {v}: {e}"]
    if r.returncode != 0:
        tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["?"])[-1]
        kind = _classify_pip_failure(tail)
        hint = ("    the package index was unreachable — the previous "
                "installation is preserved; retry when online"
                if kind == "network" else
                f"    fix manually:  {exe} -m pip install " + " ".join(_install_specs()))
        return False, kind, lines + [f"  ✖ pip install failed [{kind}] (rc={r.returncode}): "
                                     f"{tail[:240]}", hint]
    return True, "", lines + [f"  ✔ runtime {'refreshed' if refresh else 'created'}."]


# ------------------------------------------------------------------ the gate

def _windowless_target(exe: Path) -> tuple[Path | None, str | None]:
    """Final persistence exe for a candidate + a refusal reason if none.
    Windows: probe the pythonw sibling (F4); no sibling -> refuse."""
    if not _nt():
        return exe, None
    if exe.name.lower().startswith("pythonw"):
        return exe, None
    sib = exe.with_name("pythonw.exe")
    if sib.exists():
        return sib, None
    return None, (f"no pythonw.exe beside {exe} — a console-subsystem .lnk "
                  "target pops a window every logon (audit F4)")


def install_gate() -> tuple[bool, dict | None, list[str]]:
    """Choose+validate the runtime `daemon install` will persist.

    Called BEFORE anything on disk is touched. Success returns the manifest
    (already written to install.json); failure returns actionable lines and
    leaves the previous manifest/persistence exactly as they were.
    """
    from . import __version__
    lines: list[str] = []
    env = os.environ
    disabled = bool(env.get("GALVANIZE_RUNTIME_DISABLE", "").strip())
    skip = bool(env.get("GALVANIZE_RUNTIME_SKIP_PROVISION", "").strip())
    forced = (env.get("GALVANIZE_INSTALL_INTERP") or "").strip()
    chosen: dict | None = None
    source = ""

    def appvenv(exe: Path) -> dict | None:
        """Probe the app venv; on stale/broken, one provision refresh, re-probe."""
        m = probe(exe)
        if m.get("ok") and m.get("version") == __version__:
            return m
        if skip:
            lines.extend(describe_probe(m, who="app venv"))
            lines.append("  (provisioning skipped via GALVANIZE_RUNTIME_SKIP_PROVISION)")
            return None
        why = ("stale galvanize " + str(m.get("version"))
               if m.get("ok") else "failed probe")
        lines.append(f"app venv needs repair ({why}); refreshing ...")
        ok, kind, plines = provision(refresh=True)
        lines.extend(plines)
        if not ok:
            lines.append(f"  ✖ app venv repair failed [{kind}]")
            return None
        m = probe(exe)
        if m.get("ok") and m.get("version") == __version__:
            return m
        lines.extend(describe_probe(m, who="app venv (after refresh)"))
        return None

    if not disabled:
        vexe = venv_python(windowless=True)
        if vexe is not None:
            target, refuse = _windowless_target(vexe)
            if target is None:
                lines.append(f"✖ app venv: {refuse}")
            else:
                m = appvenv(target)
                if m:
                    chosen, source = m, "app-venv"
        elif not skip:
            ok, kind, plines = provision()
            lines.extend(plines)
            if ok:
                vexe = venv_python(windowless=True)
                if vexe is None:
                    lines.append(f"✖ provisioned venv has no usable interpreter "
                                 f"at {venv_dir()}")
                else:
                    m = probe(vexe)
                    if m.get("ok") and m.get("version") == __version__:
                        chosen, source = m, "app-venv"
                    else:
                        lines.extend(describe_probe(m, who="fresh app venv"))
            else:
                lines.append(f"✖ could not provision the app-owned runtime [{kind}]")

    if chosen is None:
        invoker = Path(forced) if forced else Path(sys.executable)
        target, refuse = _windowless_target(invoker)
        if target is None:
            lines.append(f"✖ invoker fallback refused: {refuse}")
            return False, None, lines
        m = probe(target)
        if m.get("ok") and m.get("version") == __version__:
            chosen, source = m, "invoker"
            lines.insert(0,
                         f"⚠ no app-owned runtime in use: FALLING BACK to the "
                         f"invoker interpreter {target} — it passed the full "
                         f"launch-equivalent probe (deps + keyring roundtrip).")
        else:
            if m.get("ok"):
                m.setdefault("failures", []).append(
                    {"check": "version", "kind": "version",
                     "detail": f"invoker env imports galvanize {m.get('version')}, "
                               f"installer is {__version__} — refusing a silent "
                               f"version retarget (F3 disease)"})
                m["ok"] = False
            lines.extend(describe_probe(m, who="invoker fallback"))

    if chosen is None:
        lines.append("✖ daemon install REFUSED: no candidate passed the "
                     "launch-equivalent probe.")
        lines.append("  Previous installation preserved: install.json and "
                     "the persistence entry (.lnk / watchdog task) were NOT "
                     "retargeted. Fix the environment above and re-run "
                     "`galvanize daemon install` (unset GALVANIZE_RUNTIME_* "
                     "overrides first if you set them for testing).")
        return False, None, lines

    home = _paths.ensure_home()
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "ts": time.time(),
        "source": source,
        "interpreter": chosen.get("exe"),
        "launch": {"cmd": [chosen.get("exe"), "-m", "galvanize.cli", "supervise"],
                   "cwd": str(home)},
        "version": chosen.get("version"),
        "python": chosen.get("python"),
        "capabilities": chosen.get("capabilities"),
        "probe": chosen,
        "written_by": {"version": __version__, "exe": sys.executable,
                       "ts": time.time()},
    }
    write_manifest(manifest)
    lines.insert(0, f"✔ runtime validated ({source}): {chosen.get('exe')} "
                   f"[galvanize {chosen.get('version')}, py {chosen.get('python')}] "
                   f"— recorded in {manifest_path()}")
    return True, manifest, lines


# ------------------------------------------------------------------ doctor: three-way identity

def _norm(p) -> str | None:
    if not p:
        return None
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except Exception:
        return None


def persistence_target() -> dict | None:
    """What the OS persistence entry ACTUALLY launches today
    {"exe","args","cwd"} — read back from the .lnk / unit / plist."""
    plat = sys.platform
    try:
        if plat == "win32":
            from . import autostart
            lnk = autostart._startup_lnk()
            if not lnk.exists():
                return None
            ps = ("$w=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                  "$w.TargetPath;$w.Arguments;$w.WorkingDirectory"
                  % str(lnk).replace("'", "''"))
            r = winlaunch.run(["powershell", "-NoProfile", "-Command", ps],
                              timeout=30)
            if r.returncode != 0:
                return None
            parts = [ln for ln in (r.stdout or "").splitlines()]
            if not parts or not parts[0].strip():
                return None
            return {"exe": parts[0].strip(),
                    "args": parts[1].strip() if len(parts) > 1 else "",
                    "cwd": parts[2].strip() if len(parts) > 2 else ""}
        if plat == "darwin":
            import plistlib
            from . import autostart
            p = autostart._plist_path()
            if not p.exists():
                return None
            with open(p, "rb") as fh:
                d = plistlib.load(fh)
            argv = d.get("ProgramArguments") or []
            return {"exe": str(argv[0]), "args": " ".join(argv[1:]), "cwd": ""} \
                if argv else None
        from . import autostart
        unit = autostart._unit_path()
        if not unit.exists():
            return None
        for ln in unit.read_text(encoding="utf-8").splitlines():
            if ln.startswith("ExecStart="):
                argv = ln[len("ExecStart="):].split()
                return {"exe": argv[0], "args": " ".join(argv[1:]), "cwd": ""} \
                    if argv else None
        return None
    except Exception:
        return None


def doctor_rows() -> list[dict]:
    """Manifest vs persistence target vs live daemon: the three-way diff
    the card requires — plus capability drift between what we installed
    and what the READY worker advertises. Rows feed manage.doctor()."""
    from . import supervisor as sup
    rows: list[dict] = []
    m = load_manifest()
    if m is None:
        rows.append({"name": "runtime", "ok": True, "warn": True,
                     "note": "no install.json — pre-C4 install? run: "
                             "galvanize daemon install (records the probed "
                             "interpreter + capabilities)"})
        return rows
    src = m.get("source")
    rows.append({"name": "runtime",
                 "ok": True, "warn": src == "invoker",
                 "note": f"{src}: {m.get('interpreter')} "
                         f"[galvanize {m.get('version')}, py {m.get('python')}]"
                         + (" — invoker fallback, not app-owned" if src == "invoker" else "")})
    if not Path(str(m.get("interpreter") or "")).exists():
        rows.append({"name": "runtime identity", "ok": False,
                     "note": f"RECORDED RUNTIME MISSING: {m.get('interpreter')} "
                             "no longer exists (venv deleted?) — re-run "
                             "daemon install, it will re-provision"})
        return rows

    snap = sup.snapshot()
    live_exe = ((snap or {}).get("worker") or {}).get("exe")
    tgt = persistence_target()
    a, b, c = _norm(m.get("interpreter")), _norm((tgt or {}).get("exe")), _norm(live_exe)
    vals = f"manifest={m.get('interpreter')} persistence={(tgt or {}).get('exe') or '—'} live={live_exe or '—'}"
    present = [x for x in (a, b, c) if x]
    if len(present) < 2:
        rows.append({"name": "runtime identity", "ok": True, "warn": True,
                     "note": "cannot diff yet: " + vals})
    elif len(set(present)) == 1 and c is not None:
        rows.append({"name": "runtime identity", "ok": True,
                     "note": f"manifest = persistence = live worker "
                             f"({m.get('interpreter')})"})
    elif c is None:
        rows.append({"name": "runtime identity", "ok": len(set(present)) == 1,
                     "warn": True,
                     "note": ("daemon not running; 2-way diff manifest vs "
                              + ("persistence: match" if len(set(present)) == 1
                                 else f"persistence: MISMATCH {vals}"))})
    else:
        rows.append({"name": "runtime identity", "ok": False,
                     "note": "MISMATCH (F3): " + vals +
                             " — fix: galvanize daemon install"})

    # capability drift: stable keys of the manifest probe vs live READY
    mcaps = {k: v for k, v in (m.get("capabilities") or {}).items()
             if k in ("imap", "folder", "keyring")}
    lcaps = {k: v for k, v in (((snap or {}).get("worker") or {}).get("capabilities") or {}).items()
             if k in ("imap", "folder", "keyring")}
    if snap is None or snap.get("state") != "ready":
        rows.append({"name": "runtime capabilities", "ok": all(bool(v) for v in mcaps.values()),
                     "warn": snap is None or not lcaps,
                     "note": ("installed: " + ", ".join(f"{k}={v}" for k, v in sorted(mcaps.items()))
                              + " (no live READY to compare)")})
    elif lcaps.keys() != mcaps.keys():
        rows.append({"name": "runtime capabilities", "ok": True, "warn": True,
                     "note": "live worker predates the C4 manifest (old code); "
                             "a supervisor restart settles it"})
    elif lcaps == mcaps:
        rows.append({"name": "runtime capabilities", "ok": all(bool(v) for v in mcaps.values()),
                     "warn": not all(bool(v) for v in mcaps.values()),
                     "note": "live READY matches manifest: "
                             + ", ".join(f"{k}={v}" for k, v in sorted(mcaps.items()))})
    else:
        rows.append({"name": "runtime capabilities", "ok": False,
                     "note": f"DRIFT live {lcaps} vs manifest {mcaps} — "
                             "restart the daemon or re-run daemon install"})
    return rows
