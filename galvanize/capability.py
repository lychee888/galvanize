"""The one capability/READINESS manifest (card C4, audit F3/F4 + judge verdict #2).

An import probe alone is NECESSARY but INSUFFICIENT: what the daemon can
actually do at launch must be described, versioned, and compared. This module
collects that description IN the candidate interpreter, and the same dict
flows through all three consumers so they can never disagree by schema:

  * ``runtime.probe()``       — launches a candidate interpreter with the
                                launch-equivalent argv (``-I -m
                                galvanize.capability --json``, scrubbed
                                PYTHONPATH, launch cwd) and parses this JSON.
  * ``daemon._readiness()``   — the worker embeds the SAME collector output
                                in its authenticated READY post, so
                                supervisor.json advertises live capabilities.
  * ``doctor``                — three-way diff: install.json manifest vs the
                                persistence target (.lnk/unit/plist) vs the
                                live daemon identity/capabilities
                                (runtime.doctor_rows).

Schema v1 (``PROBE_SCHEMA``)::

    {
      "schema": 1, "ts": float,
      "exe": sys.executable, "python": "3.x.y", "version": "<galvanize>",
      "venv_prefix": str | None, "isolated": bool,
      "capabilities": {cli, imap, folder, keyring, serve, relay, windowless},
      "versions":   {galvanize, imap_tools, keyring, watchdog},
      "keyring":    {"backend": str|None, "roundtrip": bool},
      "failures":   [{"check": str, "kind": "dependency"|"auth"|"env"|"version",
                      "detail": str}],
      "warnings":   [str],
      "ok":         bool    # False iff a BLOCKING failure exists
    }

Failure kinds are deliberately distinguishable (the card's requirement):
``dependency`` = a package cannot be imported (respawn/retry cannot fix;
refuse install). ``env`` = the interpreter/launch shape itself is wrong
(unusable exe, console-only target on Windows — audit F4). ``auth`` = the
OS keyring backend has no usable get/set/delete roundtrip (warn +
``capabilities.keyring=false``; the encrypted-file fallback still lets a
daemon run, so it never hard-refuses). ``version`` = the candidate imports a
DIFFERENT galvanize than the installer (silent downgrade is the F3 disease).
Network/auth errors at *runtime* stay transient per-watcher signals in the
READY payload (C2 semantics) — this manifest describes capability, not
connectivity.
"""

from __future__ import annotations

import json
import os
import sys
import time

PROBE_SCHEMA = 1

# Failure kinds that make a candidate interpreter UNFIT to be the daemon.
# ("auth" is intentionally NOT blocking: secrets.py has a file fallback and
# refuses to pretend a missing keyring is a missing dependency.)
BLOCKING_KINDS = frozenset({"dependency", "env", "version"})

_NT = os.name == "nt"


def _dist_version(pkg: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(pkg)
    except Exception:
        return None


def _import_check(mod: str, cap: str, dist: str, failures: list,
                 versions: dict, caps: dict) -> bool:
    """Import `mod` in THIS interpreter; record capability, version, failure.

    A module that exists but RAISES during import (the PYTHONPATH-poison
    case in C4's isolation tests) counts exactly like a missing one: a
    dependency failure, by kind.
    """
    import importlib
    try:
        importlib.import_module(mod)
        caps[cap] = True
        versions[dist] = _dist_version(dist) or "unknown"
        return True
    except BaseException as e:            # poison modules can raise anything
        caps[cap] = False
        versions[dist] = None
        failures.append({"check": f"import:{mod}", "kind": "dependency",
                         "detail": f"{type(e).__name__}: {e}"[:220]})
        return False


def keyring_capability(failures: list, warnings: list, caps: dict) -> dict:
    """Usable OS-keyring backend, proven by a fresh get/set/delete roundtrip
    (secrets.keyring_status; never the memoized runtime probe)."""
    from . import secrets as secrets_mod
    status = secrets_mod.keyring_status()
    caps["keyring"] = bool(status.get("usable"))
    if not status.get("usable"):
        entry = {"check": "keyring-backend", "kind": "auth",
                 "detail": (status.get("error")
                            or f"backend {status.get('backend')!r} has no usable "
                               "set/get/delete roundtrip")}
        if status.get("backend_missing"):
            entry["kind"] = "dependency"   # the `keyring` package itself gone
            entry["check"] = "import:keyring"
        failures.append(entry)
        if entry["kind"] == "auth":
            warnings.append(
                "keyring backend unusable — the daemon falls back to the "
                "encrypted-file store in ~/.galvanize (doctor/secrets surface).")
    return status


def windowless_capability(failures: list, caps: dict) -> None:
    """Audit F4: on Windows the persisted launch target MUST be
    pythonw.exe; a python.exe target in the .lnk = a minimized console
    every logon. The gate probes the FINAL exe, so what we see here is
    exactly what the scheduler/.lnk will run."""
    if not _NT:
        caps["windowless"] = True
        return
    name = os.path.basename(sys.executable).lower()
    ok = name.startswith("pythonw")
    caps["windowless"] = ok
    if not ok:
        failures.append({"check": "windowless-launch", "kind": "env",
                         "detail": f"target {name} is console-subsystem and no "
                                   "pythonw.exe sibling was used (audit F4: the "
                                   ".lnk would flash a console at every logon)"})


def collect() -> dict:
    """Run every check IN THIS interpreter and return the schema-v1 manifest."""
    import platform
    failures: list = []
    warnings: list = []
    caps: dict = {}
    versions: dict = {}

    # The CLI surface the launch actually uses (`-m galvanize.cli supervise`)
    _import_check("galvanize.cli", "cli", "galvanize", failures, versions, caps)
    from . import __version__
    versions["galvanize"] = __version__
    # Watcher dependencies: each maps to a trigger family's ability to run.
    _import_check("imap_tools", "imap", "imap-tools", failures, versions, caps)
    _import_check("watchdog", "folder", "watchdog", failures, versions, caps)
    kr_present = _import_check("keyring", "keyring", "keyring",
                               failures, versions, caps)
    if kr_present:
        kr = keyring_capability(failures, warnings, caps)
    else:
        caps.setdefault("keyring", False)
        kr = {"backend": None, "roundtrip": False}
    # Serve/relay: stdlib-only surfaces; cheap to assert honestly.
    try:
        import http.server  # noqa: F401
        import urllib.request  # noqa: F401
        caps["serve"] = True
        caps["relay"] = True
    except Exception as e:
        caps["serve"] = caps["relay"] = False
        failures.append({"check": "stdlib-http", "kind": "env",
                         "detail": str(e)[:220]})
    windowless_capability(failures, caps)

    return {
        "schema": PROBE_SCHEMA,
        "ts": time.time(),
        "exe": sys.executable,
        "python": platform.python_version(),
        "version": versions.get("galvanize", "?"),
        "venv_prefix": (None if sys.prefix == sys.base_prefix else sys.prefix),
        "isolated": bool(sys.flags.ignore_environment and sys.flags.no_user_site),
        "capabilities": caps,
        "versions": versions,
        "keyring": {"backend": kr.get("backend"), "roundtrip": kr.get("roundtrip")},
        "failures": failures,
        "warnings": warnings,
        "ok": not [f for f in failures if f.get("kind") in BLOCKING_KINDS],
    }


def render_lines(m: dict) -> list[str]:
    """Human lines for `galvanize probe` / installer output."""
    caps = m.get("capabilities") or {}
    marks = " ".join(f"{k}={'✔' if v else '✖'}" for k, v in sorted(caps.items()))
    out = [f"probe manifest v{m.get('schema')} — interpreter {m.get('exe')}",
           f"  python {m.get('python')}  galvanize {m.get('version')}  "
           f"venv {m.get('venv_prefix') or '(not a venv)'}  "
           f"isolated={'yes' if m.get('isolated') else 'no'}",
           f"  {marks}"]
    for f in m.get("failures") or []:
        out.append(f"  ✖ [{f.get('kind')}] {f.get('check')}: {f.get('detail')}")
    for w in m.get("warnings") or []:
        out.append(f"  ⚠ {w}")
    out.append(("  => FIT to run the daemon" if m.get("ok")
                else "  => NOT FIT (blocking failures above)"))
    return out


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="galvanize.capability",
                                description="emit the capability/READINESS manifest")
    p.add_argument("--json", action="store_true", dest="machine")
    args = p.parse_args(argv)
    m = collect()
    if args.machine:
        json.dump(m, sys.stdout)
    else:
        for ln in render_lines(m):
            print(ln)
    return 0 if m["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
