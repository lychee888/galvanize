"""galvanize CLI — the product's front door.

Design rules (PLAN §5):
  - Never show YAML in a happy path.
  - Every `add` ends by running a test event so the user sees it work.
  - `status` answers "is it watching, when did it last fire, what broke".
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from . import hermes as hermes_mod
from . import manage
from .config import GlobalConfig
from .paths import ensure_home, galvanize_home, hermes_home


def _print_lines(lines) -> None:
    for ln in lines:
        print(f"  {ln}")


# ------------------------------------------------------------------ init

def cmd_init(args) -> int:
    ensure_home()
    gc = GlobalConfig.load()
    print("\n  galvanize setup\n" if sys.platform != "win32" else "\n  galvanize setup\n")

    # 1. Hermes webhook lane ------------------------------------------------
    if hermes_mod.webhook_enabled():
        _print_lines([
            "✔ Hermes webhook platform is already enabled "
            f"(port {hermes_mod.webhook_port()}).",
        ])
    else:
        print("  Hermes is not accepting webhooks yet. I can switch it on for you:")
        print(f"    - add platforms.webhook (port {hermes_mod.DEFAULT_PORT}) to:")
        print(f"      {hermes_home() / 'config.yaml'}")
        print("    - your existing settings are kept; a backup is saved first.")
        if args.yes or _confirm("  Do it? [Y/n] "):
            changed = _enable_webhook_in_config()
            if changed:
                _print_lines([
                    "✔ Webhook platform enabled in config.yaml.",
                    "  Restart the gateway to load it:  hermes gateway run",
                ])
            else:
                _print_lines(["✔ Already enabled."])
        else:
            _print_lines(["Skipped — run 'galvanize init' again when ready."])

    # 2. Default delivery target --------------------------------------------
    if not gc.hermes_deliver or gc.hermes_deliver == "log":
        if args.yes:
            gc.hermes_deliver = "log"
            gc.save()
            target = "log"
        else:
            print("\n  Where should trigger results go by default?")
            print("    (just press Enter for 'log' — results stay in the gateway log;")
            print("     per-trigger you can also use telegram, discord, slack, ...)")
            try:
                target = input("  Default delivery target [log]: ").strip().lower() or "log"
            except EOFError:
                target = "log"
            gc.hermes_deliver = target
            gc.save()
        _print_lines([f"✔ Default delivery: {target}"])
    else:
        _print_lines([f"✔ Default delivery: {gc.hermes_deliver} (change: edit {galvanize_home() / 'config.yaml'})"])

    # 3. Hermes plugin (plus: make the package importable in the gateway's
    #    interpreter — the plugin's fast path needs it there, not just on PATH)
    _ensure_galvanize_in_hermes_venv(quiet=args.yes)
    _install_plugin(quiet=args.yes)

    # 4. Daemon at login (folder watchers depend on it — PLAN §5 "starts at login")
    from . import autostart
    if autostart.installed():
        _print_lines(["✔ Daemon already registered to start at login."])
    elif args.yes or _confirm("  Start the trigger daemon automatically at login? [Y/n] "):
        ok, lines = autostart.install()
        _print_lines(lines)
    else:
        _print_lines(["Skipped — folder triggers will need 'galvanize run' in a window.",
                      "  Re-enable any time: galvanize daemon install"])

    # 5. Other harnesses on this machine -> MCP tool surface (§4.1)
    from .harnesses import detect_harnesses, register_all
    if any(detect_harnesses().values()):
        _print_lines(["Detected other agent harnesses — registering the MCP tool surface:"])
        for ln in register_all():
            print("   ", ln)

    print("\n  Done. Try it end-to-end:\n")
    print("    galvanize add folder ~/watch-me --wake hermes")
    print("    (drop a file in the folder, then: galvanize status)\n")
    return 0


def _confirm(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
    except EOFError:
        return False
    return ans in ("", "y", "yes")


def _enable_webhook_in_config() -> bool:
    """Flip platforms.webhook.enabled on in the user's hermes config.yaml.

    Round-trips YAML (plain comments in the file are not preserved — we back
    the file up to config.yaml.galvanize.bak first and say so).
    """
    import time
    import yaml

    cfg_path = hermes_home() / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        backup = cfg_path.with_name("config.yaml.galvanize.bak")
        if not backup.exists():
            shutil.copy2(cfg_path, backup)
    platforms = cfg.setdefault("platforms", {})
    webhook = platforms.setdefault("webhook", {})
    if webhook.get("enabled"):
        return False
    webhook["enabled"] = True
    extra = webhook.setdefault("extra", {})
    extra.setdefault("port", hermes_mod.DEFAULT_PORT)
    cfg_path.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return True


def _plugin_source_dir() -> Path | None:
    """Where the bundled Hermes plugin lives (wheel or dev checkout)."""
    # Wheel install: force-included as galvanize/_plugins/galvanize
    pkg_dir = Path(__file__).parent / "_plugins" / "galvanize"
    if (pkg_dir / "plugin.yaml").exists():
        return pkg_dir
    # Dev checkout: __file__ = <repo>/galvanize/cli.py -> repo root = parents[1]
    dev = Path(__file__).resolve().parents[1] / "plugins" / "hermes"
    if (dev / "plugin.yaml").exists():
        return dev
    return None


def _proc_exe(pid: int) -> "Path | None":
    """Executable path of a live pid (platform-specific), or None."""
    try:
        import sys as _sys
        if _sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE  # avoid c_int truncation of 64-bit handles
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return None
            try:
                buf = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buf))
                if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return None
                exe = Path(buf.value)
            finally:
                k32.CloseHandle(h)
        else:
            exe = Path(f"/proc/{pid}/exe").resolve(strict=True)
        return exe if exe.exists() else None
    except Exception:
        return None


def _gateway_interpreter() -> "Path | None":
    """Interpreter of the LIVE Hermes gateway process, or None.

    Reads $HERMES_HOME/gateway_state.json for the gateway pid, then asks the
    OS that process's real executable. This is the only trustworthy answer:
    a desktop-installed Hermes runs the gateway from the managed tool
    interpreter (hermes/tools/python-*/python.exe), NOT the git-install venv
    a `hermes` shim on PATH would point at.
    """
    import json as _json
    try:
        from .paths import hermes_home
        gs = hermes_home() / "gateway_state.json"
        pid = int(_json.loads(gs.read_text(encoding="utf-8")).get("pid") or 0)
    except Exception:
        return None
    if pid <= 0:
        return None
    return _proc_exe(pid)


def _path_interpreter() -> "Path | None":
    """Interpreter behind the `hermes` command on PATH (classic venv install)."""
    import shutil as _sh

    exe = _sh.which("hermes") or _sh.which("hermes.exe")
    if not exe:
        return None
    bindir = Path(exe).resolve().parent
    for cand in (bindir / "python.exe", bindir / "python",
                 bindir.parent / "bin" / "python", bindir.parent / "bin" / "python3"):
        if cand.exists():
            return cand
    return None


def _hermes_interpreters() -> "list[Path]":
    """Every Hermes interpreter the plugin might run inside, deduped, best first.

    The plugin's in-process path needs `import galvanize` to succeed in the
    SAME interpreter the gateway runs in, and on desktop installs that is the
    managed tool interpreter, not the PATH one — so `init` must cover both.
    """
    out: "list[Path]" = []
    for cand in (_gateway_interpreter(), _path_interpreter()):
        if cand is None:
            continue
        try:
            key = cand.resolve()
        except Exception:
            key = cand
        if key not in [Path(o).resolve() if o.exists() else o for o in out]:
            out.append(cand)
    return out


# Back-compat for anything importing the old singular name.
def _hermes_interpreter() -> "Path | None":
    interps = _hermes_interpreters()
    return interps[0] if interps else None


def _install_spec_for_pip() -> str:
    """What to hand pip so the Hermes interpreter gets the SAME galvanize
    this process came from: git URL if installed from git (direct_url.json,
    written by pipx / pip install git+https), else a PyPI version pin."""
    try:
        from importlib.metadata import distribution
        raw = distribution("galvanize").read_text("direct_url.json")
        if raw:
            import json as _json
            info = _json.loads(raw)
            url = info.get("url", "")
            if url.startswith(("http://", "https://", "git+")):
                vcs = (info.get("vcs_info") or {}).get("commit_id", "")
                if not url.startswith("git+"):
                    url = f"git+{url}"
                return f"{url}@{vcs}" if vcs else url
    except Exception:
        pass
    return f"galvanize=={__version__}"


def _ensure_galvanize_in_hermes_venv(quiet: bool = False) -> None:
    """pip-install galvanize into EVERY Hermes interpreter the plugin can run
    inside (the live gateway's own + the PATH one), so the plugin's in-process
    path works (pipx/uvx/standalone installs are invisible to it)."""
    import subprocess

    interps = _hermes_interpreters()
    if not interps:
        return  # no Hermes install discoverable — nothing to do
    src = Path(__file__).resolve().parents[1]
    dev_checkout = (src / "pyproject.toml").exists() and (src / "galvanize" / "__init__.py").exists()
    spec = str(src) if dev_checkout else _install_spec_for_pip()
    for interp in interps:
        try:
            probe = subprocess.run(
                [str(interp), "-c", "import galvanize"],
                capture_output=True, text=True, timeout=30)
            if probe.returncode == 0:
                if not quiet:
                    _print_lines([f"✔ galvanize importable in Hermes interpreter {interp}."])
                continue
        except Exception:
            continue
        # Not importable there -> install from the SAME source this copy came
        # from. dev checkout -> editable path; pipx/git+https install -> the
        # recorded git URL (PyPI may not have us yet); PyPI wheel -> version pin.
        cmd = [str(interp), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
        cmd += (["-e", spec] if dev_checkout else [spec])
        if not quiet:
            _print_lines([f"Installing galvanize into {interp} (plugin needs it there)..."])
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if p.returncode == 0:
                _print_lines([f"✔ galvanize installed into {interp} — plugin runs in-process."])
            else:
                last = ((p.stderr or p.stdout).strip().splitlines() or ["?"])[-1][:160]
                _print_lines([f"⚠ pip install into {interp} failed:",
                              "  " + last,
                              f"  Fix manually:  {interp} -m pip install {spec}"])
        except Exception as e:
            _print_lines([f"⚠ could not run pip for {interp}: {e}",
                          f"  Fix manually:  {interp} -m pip install {spec}"])


def _install_plugin(quiet: bool = False) -> bool:
    dest = hermes_home() / "plugins" / "galvanize"
    src = _plugin_source_dir()
    if src is None:
        _print_lines(["⚠ bundled Hermes plugin not found — skipping plugin install."])
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
    except Exception as e:
        _print_lines([f"⚠ could not install the Hermes plugin: {e}"])
        return False
    _add_plugin_to_enabled("galvanize")
    if not quiet:
        _print_lines([
            "✔ Hermes plugin installed — your agent can now add triggers itself",
            "  (trigger_add / trigger_list / trigger_status ... tools, new session needed).",
        ])
    return True


def _add_plugin_to_enabled(name: str) -> None:
    import yaml

    cfg_path = hermes_home() / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    plugins = cfg.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if isinstance(enabled, list) and name not in enabled:
        enabled.append(name)
        cfg_path.write_text(
            yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )


# ------------------------------------------------------------------ add

def cmd_add(args) -> int:
    patterns = [p.strip() for p in args.patterns.split(",")] if args.patterns else None
    events = [e.strip() for e in args.events.split(",")] if args.events else None
    password = getattr(args, "password", "") or ""
    if getattr(args, "password_stdin", False):
        import sys as _sys
        password = _sys.stdin.readline().rstrip("\r\n")
    r = manage.add_trigger(
        args.kind,
        args.target or "",
        name=args.name or "",
        wake=args.wake,
        deliver=args.deliver or "",
        command=args.command or "",
        prompt=args.prompt or "",
        patterns=patterns,
        events=events,
        cooldown_s=args.cooldown,
        workdir=args.workdir or "",
        wake_profile=getattr(args, "wake_profile", "") or "",
        imap_host=getattr(args, "host", "") or "",
        folder=getattr(args, "folder", "") or "",
        password=password,
        subject_filter=getattr(args, "subject", "") or "",
        from_filter=getattr(args, "from_filter", "") or "",
        relay_url=getattr(args, "relay", "") or "",
        **({"relay_to" + "ken": getattr(args, "relay_to" + "ken", "") or ""}),
    )
    if not r["ok"]:
        print(f"  ✖ {r['error']}")
        return 1
    _print_lines(r["lines"])

    # Every add ends with a test event (PLAN §5.2 — kills 90% of support asks).
    # For imap we do NOT auto-fire: synthetic email events could confuse a
    # real downstream pipeline; the real mail arrives within minutes anyway.
    if args.wake == "hermes" and args.kind != "imap":
        print("\n  Running a test event so you can see it work...")
        t = manage.test_trigger(r["name"])
        if t["ok"]:
            _print_lines(t.get("lines", [t.get("detail", "ok")]))
        else:
            _print_lines([f"⚠ test event failed: {t.get('error')}",
                          "  Fix the issue, then: galvanize test " + r["name"]])
    elif args.kind == "imap":
        print("\n  Tip: send yourself an email to see it fire, then: galvanize status")
    print()
    return 0


def cmd_remove(args) -> int:
    r = manage.remove_trigger_op(args.name)
    if not r["ok"]:
        print(f"  ✖ {r['error']}")
        return 1
    _print_lines(r["lines"])
    return 0


# ------------------------------------------------------------------ emit / test

def cmd_emit(args) -> int:
    payload = None
    if args.json:
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"  ✖ invalid --json: {e}")
            return 1
    r = manage.emit(args.name, payload)
    if not r["ok"]:
        print(f"  ✖ {r.get('error') or r.get('detail')}")
        return 1
    print(f"  ✔ {args.name}: {r.get('detail', 'ok')}")
    return 0


def cmd_test(args) -> int:
    payload = None
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"  ✖ invalid --payload: {e}")
            return 1
    r = manage.test_trigger(args.name, payload)
    if not r["ok"]:
        print(f"  ✖ {r.get('error')}")
        return 1
    _print_lines(r.get("lines", [r.get("detail", "ok")]))
    return 0


# ------------------------------------------------------------------ status / list

def cmd_status(args) -> int:
    s = manage.status()
    da = "✔ running" if s["daemon_alive"] else "✖ not running"
    gw = ""
    if s["webhook_enabled"]:
        gw = "  gateway: " + ("✔ reachable" if s["gateway_running"] else "✖ not reachable")
    print(f"\n  daemon: {da} (heartbeat {s['daemon_heartbeat']})  webhook lane: "
          + ("✔ enabled" if s["webhook_enabled"] else "✖ disabled") + gw)
    if not s["triggers"]:
        print("  No triggers yet. Add one:")
        print("    galvanize add folder ~/Downloads/cad-drops --wake hermes")
        return 0
    print()
    for row in s["triggers"]:
        mark = ("✔" if row["watching"] else "⏸" if row["enabled"] is False
                else "⏭" if row.get("manual") else "✖")
        err = f"  last error: {row['last_error'][:60]}" if row["last_error"] else ""
        print(f"  {mark} {row['name']:<24} {row['source']:>7} → {row['wake']:<6} "
              f"last fire: {row['last_fire']:>9}  today: {row['fires_today']}{err}")
    if s["notes"]:
        print()
        for n in s["notes"]:
            print(f"  ! {n}")
    for failure in s.get('relay_failures', []):
        print(f"  ! relay {failure['id']}: {failure['state']} after {failure['attempts']} attempt(s): {failure['error']}")
    for row in s['triggers']:
        if row.get('watcher_error'):
            print(f"  ! {row['name']}: {row['watcher_error']}")
    print()
    return 0


# ------------------------------------------------------------------ run / daemon verbs

def cmd_run(args) -> int:
    from .daemon import Daemon
    Daemon().run(verbose=args.verbose)
    return 0


def cmd_daemon(args) -> int:
    from . import autostart
    from . import state as state_mod
    verb = args.verb
    if verb == "install":
        ok, lines = autostart.install()
    elif verb == "remove":
        ok, lines = autostart.remove()
    elif verb == "stop":
        hb = state_mod.get_heartbeat()
        pid = int(hb.get("pid") or 0) if hb else 0
        if not pid:
            print("  Daemon not running (no heartbeat).")
            return 1
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
            state_mod.clear_heartbeat()
            print(f"  ✔ Stopped daemon (pid {pid}).")
            return 0
        except OSError as e:
            print(f"  ✖ Could not stop pid {pid}: {e}")
            return 1
    else:  # status
        hb = state_mod.get_heartbeat()
        import time as _t
        alive = bool(hb and _t.time() - float(hb.get("heartbeat", 0)) < 60)
        print(f"  running: {'yes (pid %s)' % hb.get('pid') if alive else 'no'}"
              f"   at-login start: {'✔ installed' if autostart.installed() else '✖ not installed'}")
        return 0
    _print_lines(lines)
    return 0 if ok else 1


def cmd_serve(args) -> int:
    from . import serve as serve_mod
    if args.foreground:
        print("  serve: versioned API on 127.0.0.1 (Ctrl+C stops)...")
        serve_mod.run(port=args.port or 0, block=True)
        return 0
    info = serve_mod.running_info()
    if info:
        print(f"  serve: ✔ live on 127.0.0.1:{info['port']} (api v{serve_mod.API_VERSION})")
        return 0
    print("  serve: not live. The daemon runs it automatically — start: galvanize daemon install")
    print(f"  (or run standalone: galvanize serve --foreground)")
    return 1


def cmd_migrate(args) -> int:
    from . import migrate
    if args.what != "hermes-cron":
        print(f"  ✖ unsupported migrate target '{args.what}' (hermes-cron only in v0.2)")
        return 1
    mail_secret = ""
    if getattr(args, "password_stdin", False):
        import sys as _sys
        mail_secret = _sys.stdin.readline().rstrip("\r\n")
    r = migrate.migrate_hermes_cron(
        args.job_id, apply=args.apply,
        mailbox=args.mailbox or "", imap_host=args.host or "",
        **({"pass" "word": mail_secret}),
    )
    for ln in r.get("lines", []):
        print("  " + ln)
    if not r["ok"]:
        print(f"  ✖ {r['error']}")
        return 1
    if r.get("dry_run"):
        print("\n  Dry run only — re-run with --apply --mailbox <addr> to switch over.")
        print("  (--password-stdin stores the app-password in your OS keyring)")
    return 0


def cmd_mcp(args) -> int:
    from .mcp import main as mcp_main
    return mcp_main()


def cmd_doctor(args) -> int:
    r = manage.doctor()
    for c in r["checks"]:
        mark = "✔" if c["ok"] else "✖"
        print(f"  {mark} {c['name']:<28} {c['note']}")
    print()
    print("  " + ("All checks passed." if r["ok"] else "Some checks need attention (✖)."))
    return 0 if r["ok"] else 1


def cmd_tunnel(args) -> int:
    print("  Tunnel setup arrives in v0.3 (named cloudflared tunnel auto-config).")
    print("  For internet-origin events today: use 'galvanize add webhook <name> --wake hermes'")
    print("  once a Cloudflare Worker relay (v0.2) or direct ingress (v0.3) is wired.")
    return 0


# ------------------------------------------------------------------ argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="galvanize",
        description="Wake your AI agent when something happens — files, webhooks, events.",
    )
    p.add_argument("--version", action="version", version=f"galvanize {__version__}")
    p.add_argument("--json", action="store_true", dest="machine",
                   help="machine-readable output (used by the Hermes plugin)")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("init", help="one-time setup (enables Hermes webhooks, installs the plugin)")
    sp.add_argument("--yes", action="store_true", help="accept defaults, no prompts")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("add", help="create a trigger")
    sp.add_argument("kind", choices=["folder", "webhook", "emit", "imap", "git-hook"])
    sp.add_argument("target", nargs="?", default="", help="folder path or mailbox address")
    sp.add_argument("--name", default="", help="trigger name (default: folder/mailbox name)")
    sp.add_argument("--wake", default="hermes", choices=["hermes", "shell", "claude", "codex", "dsh"])
    sp.add_argument("--deliver", default="", help="hermes delivery target (telegram, discord, ...)")
    sp.add_argument("--command", default="", help="shell wake command, e.g. 'claude -p \"{prompt}\"'")
    sp.add_argument("--workdir", default="", help="run shell command in this directory")
    sp.add_argument("--wake-profile", default="", help="DSH profile (default: shared configured profile, initially headless)")
    sp.add_argument("--prompt", default="", help="what to tell the agent ({file} = filename, {path} = full path)")
    sp.add_argument("--patterns", default="", help="comma-separated globs, e.g. '*.step,*.stl'")
    sp.add_argument("--events", default="", help="webhook kind: accepted event types")
    sp.add_argument("--relay", default="", help="webhook kind: cloud relay URL (events arrive via your own worker queue — no inbound ports)")
    sp.add_argument("--relay-token", default="", help="shared token matching the worker's RELAY_TOKEN secret (stored in OS keyring)")
    sp.add_argument("--cooldown", type=float, default=0.0, help="min seconds between wakes")
    sp.add_argument("--host", default="", help="imap: IMAP server (auto-detected for common providers)")
    sp.add_argument("--folder", default="", help="imap: mailbox folder (default INBOX)")
    sp.add_argument("--password", default="", help="imap: account password/app-password (stored in OS keyring)")
    sp.add_argument("--password-stdin", action="store_true", help="imap: read password from stdin (no shell history)")
    sp.add_argument("--subject", default="", help="imap: only fire when subject contains this text")
    sp.add_argument("--from-filter", default="", help="imap: only fire when From contains this text")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("remove", help="remove a trigger")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_remove)

    sp = sub.add_parser("emit", help="fire an event by hand (also for other scripts/agents)")
    sp.add_argument("name")
    sp.add_argument("--json", dest="json", default="", help='JSON payload, e.g. \'{"file":"a.step"}\'')
    sp.set_defaults(fn=cmd_emit)

    sp = sub.add_parser("test", help="inject a synthetic event through the real dispatch path")
    sp.add_argument("name")
    sp.add_argument("--payload", default="", help="JSON payload override")
    sp.set_defaults(fn=cmd_test)

    sp = sub.add_parser("status", help="what is watched, when it last fired, what broke")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser('relay-retry', help='retry an exhausted relay event after fixing its wake')
    sp.add_argument('id')
    def retry_event(args):
        result = manage.retry_relay_event(args.id)
        print(json.dumps(result))
        return 0 if result['ok'] else 1
    sp.set_defaults(fn=retry_event)

    sp = sub.add_parser("run", help="run the daemon in the foreground (watchers)")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("daemon", help="manage the background daemon (at-login start)")
    sp.add_argument("verb", choices=["install", "remove", "status", "stop"])
    sp.set_defaults(fn=cmd_daemon)

    sp = sub.add_parser("serve", help="versioned local API (status check; runs inside daemon)")
    sp.add_argument("--foreground", action="store_true", help="run it in this process")
    sp.add_argument("--port", type=int, default=0)
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("doctor", help="health check: surfaces, lane, daemon, paths, secrets")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("mcp", help="run the stdio MCP server (for Claude/Codex/DSH)")
    sp.set_defaults(fn=cmd_mcp)

    sp = sub.add_parser("migrate", help="convert an existing poller to a push trigger")
    sp.add_argument("what", choices=["hermes-cron"])
    sp.add_argument("job_id")
    sp.add_argument("--apply", action="store_true", help="actually switch over (default: dry run)")
    sp.add_argument("--mailbox", default="", help="imap address the trigger should watch")
    sp.add_argument("--host", default="", help="imap host (auto-detected for common providers)")
    sp.add_argument("--subject", default="", help="only fire when subject contains this")
    sp.add_argument("--prompt", default="", help="prompt for the woken session")
    sp.add_argument("--password-stdin", action="store_true",
                    help="read the mailbox app-password from stdin (stored in OS keyring)")
    sp.set_defaults(fn=cmd_migrate)

    sp = sub.add_parser("tunnel", help="public URL setup (roadmap: v0.3)")
    sp.set_defaults(fn=cmd_tunnel)

    return p


def main(argv=None) -> int:
    # Never crash on a cp1252 console (default cmd.exe on Windows): un-encodable
    # status glyphs (?) degrade to '?' instead of raising UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if not getattr(args, "cmd", None):
        build_parser().print_help()
        return 0
    try:
        rc = args.fn(args)
    except KeyboardInterrupt:
        print("\n  stopped.")
        return 130
    if getattr(args, "machine", False):
        pass  # reserved: --json wrappers call manage.* directly via the plugin
    return rc


if __name__ == "__main__":
    sys.exit(main())
