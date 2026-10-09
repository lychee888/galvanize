"""Trigger management operations shared by CLI and the Hermes plugin.

One implementation of add/remove/test/status semantics, so the agent and a
human at a terminal cannot drift apart. All functions return plain dicts so
callers can render (CLI) or json.dumps (plugin tool handler).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import hermes as hermes_mod
from . import state as state_mod
from .bus import TriggerBus
from .config import (
    GlobalConfig,
    Trigger,
    load_triggers,
    new_secret,
    remove_trigger,
    upsert_trigger,
    valid_name,
)
from .events import Event


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_age(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _guess_imap_host(address: str) -> str:
    """Best-guess IMAP host from the mailbox domain (common providers,
    then the conventional imap.<domain>). The first real connect verifies;
    a wrong guess surfaces as a clear watcher error, and the caller can
    always pass --host explicitly."""
    domain = address.split("@")[-1].lower()
    known = {
        "gmail.com": "imap.gmail.com",
        "googlemail.com": "imap.gmail.com",
        "outlook.com": "outlook.office365.com",
        "hotmail.com": "outlook.office365.com",
        "live.com": "outlook.office365.com",
        "yahoo.com": "imap.mail.yahoo.com",
    }
    return known.get(domain, f"imap.{domain}")


def add_trigger(
    kind: str,
    target: str = "",
    *,
    name: str = "",
    wake: str = "hermes",
    deliver: str = "",
    command: str = "",
    prompt: str = "",
    patterns: Optional[List[str]] = None,
    events: Optional[List[str]] = None,
    cooldown_s: float = 0.0,
    workdir: str = "",
    description: str = "",
    imap_host: str = "",
    folder: str = "",
    password: str = "",
    subject_filter: str = "",
    from_filter: str = "",
    relay_url: str = "",
    relay_token: str = "",
    wake_profile: str = "",
) -> Dict[str, Any]:
    """Create a trigger. kind: folder | webhook | emit | imap.

    Returns {"ok", "name", "lines": [...human summary...], "url": ...}.
    For wake=hermes this also registers the HMAC route on the Hermes lane.
    Password for imap is stored via secrets.py (OS keyring), never in YAML.
    """
    name = (name or "").strip().lower().replace(" ", "-")
    if not name and kind == "folder":
        name = Path(target).name.lower().replace(" ", "-")
    if not name and kind == "imap" and "@" in (target or ""):
        name = target.split("@")[0].replace(".", "-").lower()
    if not name and kind == "git-hook":
        from .sources.githook import repo_root as _rr
        _root = _rr(target or "")
        name = _root.name.lower().replace(" ", "-") if _root else ""
    if not valid_name(name):
        return {"ok": False, "error":
                f"invalid name '{name}' — use lowercase letters, digits, hyphens"}

    existing = load_triggers()
    is_update = name in existing

    source: Dict[str, Any] = {"type": kind}
    if kind == "folder":
        p = Path(target).expanduser() if target else None
        if not p:
            return {"ok": False, "error": "folder kind needs a path, e.g. galvanize add folder ~/watch --wake hermes"}
        if not p.is_dir():
            # UX: a watched folder that doesn't exist yet is the normal case
            # ("watch where downloads WILL land") — create it, don't lecture.
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"ok": False, "error": f"cannot create folder {p}: {e}"}
        source["path"] = str(p)
        if patterns:
            source["patterns"] = list(patterns)
    elif kind == "webhook":
        if events:
            source["events"] = list(events)
        if relay_url:
            # events arrive via the user's cloud worker queue (pull source),
            # not the Hermes lane — the emit trigger name == the ingest route
            source["relay"] = True
            gc = GlobalConfig.load()
            if gc.relay_url and gc.relay_url.rstrip("/") != relay_url.rstrip("/"):
                return {"ok": False, "error":
                        f"a different relay is already configured ({gc.relay_url}); "
                        "one worker per install in v0.2"}
    elif kind == "imap":
        # target = account email; host auto-detected unless given
        if not target or "@" not in target:
            return {"ok": False, "error": "imap needs the mailbox address, e.g. you@example.com"}
        host = imap_host or _guess_imap_host(target)
        if not host:
            return {"ok": False, "error":
                    f"could not auto-detect an IMAP server for {target.split('@')[-1]} — pass --host"}
        source["host"] = host
        source["user"] = target
        if folder and folder != "INBOX":
            source["folder"] = folder
        if password:
            from . import secrets as secrets_mod
            key = f"imap:{target}"
            secrets_mod.set_secret(key, password)
            secrets_mod.index_keyring_key(key)
            source["secret_key"] = key
        elif not (name in existing and existing[name].source.get("secret_key")):
            return {"ok": False, "error":
                    "no password given — pass --password (it is stored in your OS keyring, never in the YAML)"}
        if subject_filter:
            source["filter_subject"] = subject_filter
        if from_filter:
            source["filter_from"] = from_filter
    elif kind == "git-hook":
        # an emit trigger whose hooks do the emitting — the bus/wake path is
        # identical to emit, so the trigger's source stays "emit"
        from .sources.githook import repo_root as _rr2
        root = _rr2(target or "")
        if root is None:
            return {"ok": False, "error": f"not a git repository: {target or '(none given)'}"}
        source["type"] = "emit"
        source["repo"] = str(root)
    elif kind == "emit":
        pass
    else:
        return {"ok": False, "error": f"unknown source kind '{kind}'"}

    wake_cfg: Dict[str, Any] = {"kind": wake}
    from .config import WAKE_PRESETS
    if wake in WAKE_PRESETS:
        preset = WAKE_PRESETS[wake]
        if wake == "dsh":
            import re
            profile = wake_profile or GlobalConfig.load().dsh_wake_profile
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", profile):
                return {"ok": False, "error": "invalid DSH wake profile"}
            preset = f'dsh --profile {profile} "{{prompt}}"'
        wake_cfg = {"kind": "shell", "command": command or preset,
                    "harness": wake}
        if workdir:
            wake_cfg["workdir"] = workdir
        wake = f"shell({wake})"
    if wake_cfg["kind"] == "hermes":
        gc = GlobalConfig.load()
        wake_cfg["deliver"] = deliver or gc.hermes_deliver or "log"
    elif wake_cfg["kind"] == "shell":
        if command:
            wake_cfg["command"] = command   # explicit --command wins over a preset
        if not wake_cfg.get("command"):
            return {"ok": False, "error": "shell wake needs --command"}
        if workdir:
            wake_cfg["workdir"] = workdir
    else:
        return {"ok": False, "error": f"unknown wake kind '{wake}'"}

    if kind == "webhook" and wake_cfg["kind"] != "hermes" and not source.get("relay"):
        return {"ok": False, "error": "Webhook shell wakes need a relay receiver (--relay); no webhook URL was created"}

    t = Trigger(
        name=name,
        source=source,
        wake=wake_cfg,
        prompt=prompt,
        cooldown_s=float(cooldown_s or 0),
        dedupe_key="{path}" if kind == "folder" else "",
        description=description or (f"files in {target}" if kind == "folder" else ""),
        created_at=(existing.get(name).created_at if name in existing else _now_iso()),
    )
    errs = t.validate()
    if errs:
        return {"ok": False, "error": "; ".join(errs)}

    lines: List[str] = []
    url = None
    if wake == "hermes":
        secret = route_secret_or_new(name)
        # webhook kind: accept all event types on the route — external
        # services bring their own event names, and the lane's filter would
        # otherwise drop them (empty list = allow all).
        route_events = [] if kind == "webhook" else ["galvanize"]
        try:
            url = hermes_mod.register_route(
                name, secret, deliver=wake_cfg["deliver"],
                description=t.description, events=route_events,
                prompt=(t.prompt if kind == "webhook" else "{prompt}"),
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if kind == "webhook":
            lines.append(f"Webhook route registered: {url}")
            lines.append("Give this URL + the HMAC secret to the source service "
                         "(GitHub/Stripe/etc.) — events wake Hermes directly, no daemon needed.")
        else:
            lines.append(f"Route registered on the Hermes webhook lane: {url}")
        if not hermes_mod.webhook_enabled():
            lines.append(
                "NOTE: Hermes webhook platform is not enabled yet — run 'galvanize init' "
                "or enable platforms.webhook, then restart the gateway."
            )
        elif not hermes_mod.gateway_running():
            lines.append("NOTE: the Hermes gateway is not answering /health right now. "
                         "Start it with: hermes gateway run")

    upsert_trigger(t)
    if kind == "git-hook":
        from .sources.githook import install_hooks
        hk = install_hooks(source["repo"], name)
        if not hk["ok"]:
            remove_trigger(name)
            if wake == "hermes":
                hermes_mod.unregister_route(name)
            return {"ok": False, "error": hk["error"]}
    elif kind == "webhook" and source.get("relay"):
        from .config import GlobalConfig as _GC
        gc = _GC.load()
        gc.relay_url = relay_url.rstrip("/")
        gc.save()
        if relay_token:
            from . import secrets as _sec
            _sec.set_secret("relay:token", relay_token)
            _sec.index_keyring_key("relay:token")
    lines.insert(0, f"{'Updated' if is_update else 'Added'} trigger '{name}' "
                    f"({kind} -> {wake})")
    if kind == "git-hook":
        lines.append("Installed post-commit/post-merge hooks in " + source["repo"])
        lines.append("Every commit there wakes your agent (recursion-guarded: "
                     "commits made BY a woken agent don't re-fire).")
        lines.append("Remove: galvanize remove " + name + " clears the hooks too.")
    elif kind == "folder":
        lines.append(f"Watching: {source['path']}"
                     + (f" ({', '.join(source['patterns'])})" if patterns else ""))
        lines.append("The daemon picks it up automatically within a few seconds.")
    elif kind == "webhook" and not source.get("relay"):
        lines.append("Events arrive straight from the source service into Hermes — "
                     "no daemon needed for this trigger.")
    elif kind == "webhook":
        url = f"{relay_url.rstrip('/')}/ingest/{name}"
        lines.append(f"Queue URL for the source service: {relay_url.rstrip('/')}/ingest/{name}")
        lines.append("Configure the worker INGEST_TOKEN and send Authorization: Bearer <INGEST_TOKEN> on submissions.")
        lines.append("Your daemon pulls the queue — keep it running (it already starts at login).")
    elif kind == "imap":
        lines.append(f"Watching {source['user']}@{source['host']}:"
                     f"{source.get('folder', 'INBOX')} (password lives in your OS "
                     "credential store, never in the YAML). The daemon picks it up "
                     "within a few seconds.")
    elif kind == "emit":
        lines.append(f"Fire it with: galvanize emit {name} --json '{{...}}'")
    return {"ok": True, "name": name, "lines": lines, "url": url, "updated": is_update}


def route_secret_or_new(name: str) -> str:
    return hermes_mod.route_secret(name) or new_secret()


def remove_trigger_op(name: str) -> Dict[str, Any]:
    name = name.strip().lower()
    t = load_triggers().get(name)
    if t is None:
        return {"ok": False, "error": f"no trigger named '{name}'"}
    if t.wake_kind == "hermes":
        if not hermes_mod.unregister_route(name):
            return {"ok": False, "error":
                    f"route '{name}' on the Hermes lane was not created by galvanize; refusing to delete it"}
    extra = []
    repo = t.source.get("repo")
    if repo:
        from .sources.githook import uninstall_hooks
        hk = uninstall_hooks(str(repo))
        if hk.get("removed"):
            extra.append(f"removed {len(hk['removed'])} git hook(s) in {repo}")
    remove_trigger(name)
    return {"ok": True, "lines": [f"Removed trigger '{name}'"
                                  + (" and its Hermes route" if t.wake_kind == "hermes" else "")]
                              + extra}


def test_trigger(name: str, payload: Optional[dict] = None) -> Dict[str, Any]:
    """Inject a synthetic event through the real dispatch path."""
    name = name.strip().lower()
    ts = load_triggers()
    t = ts.get(name)
    if t is None:
        return {"ok": False, "error": f"no trigger named '{name}'"}
    evt = Event(
        trigger_name=name,
        source="test",
        type="test.synthetic",
        payload=payload or {"test": True, "name": "test-sample", "path": "(synthetic)"},
    )
    bus = TriggerBus()
    ok, detail = bus.handle(t, evt)
    r: Dict[str, Any] = {"ok": ok, "name": name, "detail": detail}
    if ok and t.wake_kind == "hermes":
        r["lines"] = [
            "Event accepted by the Hermes gateway — a fresh session is running.",
            "Watch it in the Hermes sessions log; results go to the route's deliver target.",
        ]
    elif ok:
        r["lines"] = [f"Dispatched: {detail}"]
    else:
        r["error"] = detail
    return r


def emit(name: str, payload: Optional[dict], source: str = "emit") -> Dict[str, Any]:
    """`galvanize emit` — fire an event inline (works with no daemon running)."""
    name = name.strip().lower()
    ts = load_triggers()
    t = ts.get(name)
    if t is None:
        return {"ok": False, "error": f"no trigger named '{name}' (create one: galvanize add emit {name})"}
    if not t.enabled:
        return {"ok": True, "name": name, "detail": "trigger disabled — event dropped"}
    evt = Event(trigger_name=name, source=source,
                type=str((payload or {}).get("type", "manual.emit")),
                payload=payload or {})
    # Respect cooldown (throttle spam), ignore dedupe (caller means it).
    ok, detail = TriggerBus().handle(t, evt)
    return {"ok": ok, "name": name, "detail": detail}


def status() -> Dict[str, Any]:
    """Everything `galvanize status` and the trigger_status tool render from."""
    ts = load_triggers()
    hb = state_mod.get_heartbeat()
    daemon_age = _fmt_age(hb.get("heartbeat") if hb else None)
    daemon_alive = bool(hb and (time.time() - float(hb.get("heartbeat", 0)) < 60))
    folder_count = sum(1 for t in ts.values() if t.enabled and t.source_type == "folder")

    rows: List[Dict[str, Any]] = []
    for name, t in sorted(ts.items()):
        st = state_mod.get(name)
        health = {}
        if t.source_type in ('folder', 'imap') or t.source.get('relay'):
            key = 'relay' if t.source.get('relay') else f'{t.source_type}:{name}'
            health = state_mod.source_health(key, hb.get('pid') if hb else None)
            watching = daemon_alive and bool(health.get('connected'))
        elif t.source_type == 'webhook':
            watching = hermes_mod.webhook_enabled() and hermes_mod.gateway_running()
        else:
            watching = False  # manual emit has no background listener
        row = {
            "name": name,
            "source": t.source_type,
            "wake": t.wake_kind,
            "enabled": t.enabled,
            "description": t.description,
            "prompt": t.prompt,
            "watching": bool(t.enabled and watching),
            "watcher_error": health.get('error') if health else None,
            "last_fire": _fmt_age(st.get("last_fire")),
            # fires_today only resets on the NEXT fire (record_fire), so a
            # trigger quiet for days used to report yesterday's count as
            # today's. Trust the stored date, not the counter.
            "fires_today": (int(st.get("fires_today", 0) or 0)
                            if st.get("fires_date") == date.today().isoformat() else 0),
            "last_error": st.get("last_error"),
        }
        if t.source_type == "emit":
            # Not a watcher by design: woken externally via `galvanize emit`.
            # Renderers should show a distinct 'manual' mark, not an error.
            row["manual"] = True
        if t.source_type == "folder":
            row["path"] = t.source.get("path")
        if t.source_type == "imap":
            row["mailbox"] = f"{t.source.get('user')}@{t.source.get('host')}:{t.source.get('folder', 'INBOX')}"
        rows.append(row)

    notes: List[str] = []
    if folder_count and not daemon_alive:
        notes.append(f"{folder_count} folder trigger(s) need the daemon — start: galvanize run")
    wh_enabled = hermes_mod.webhook_enabled()
    if any(t.wake_kind == "hermes" for t in ts.values()):
        if not wh_enabled:
            notes.append("Hermes webhook platform disabled — run: galvanize init")
        elif not hermes_mod.gateway_running():
            notes.append("Hermes gateway not answering /health — start: hermes gateway run")

    return {
        "ok": True,
        "daemon_alive": daemon_alive,
        "daemon_heartbeat": daemon_age,
        "webhook_enabled": wh_enabled,
        "gateway_running": (hermes_mod.gateway_running() if wh_enabled else False),
        "triggers": rows,
        "notes": notes,
        "relay_failures": relay_failures(),
    }


def relay_failures() -> list:
    from .relay_state import RelayState
    url = GlobalConfig.load().relay_url
    return RelayState(url).failures() if url else []


def retry_relay_event(event_id: str) -> dict:
    from .relay_state import RelayState
    url = GlobalConfig.load().relay_url
    ok = bool(url and RelayState(url).retry(event_id))
    return {'ok': ok, **({} if ok else {'error': 'No failed event with that id'})}


# ------------------------------------------------------------------ doctor

def doctor() -> Dict[str, Any]:
    """Health of the whole install: surfaces, lane, daemon, paths, secrets.
    Exactly-one-surface rule (§4.1) is enforced here."""
    from . import secrets as secrets_mod
    from . import serve as serve_mod
    from .paths import hermes_home

    checks = []
    def add(name, ok, note=""):
        checks.append({"name": name, "ok": bool(ok), "note": note})

    s = status()
    ts = load_triggers()

    # surfaces
    surfaces = serve_mod.read_surfaces()
    hermes_plugin = (hermes_home() / "plugins" / "galvanize" / "plugin.yaml").exists()
    hermes_mcp = surfaces.get("hermes") == "mcp" or (
        surfaces.get("hermes") is None and _hermes_config_has_mcp(hermes_home()))
    if hermes_plugin and hermes_mcp:
        add("surface:hermes", False,
            "BOTH native plugin and MCP registered — remove one (plugin preferred)")
    else:
        add("surface:hermes", True, "plugin" if hermes_plugin else ("mcp" if hermes_mcp else "none"))
    for harness in ("claude", "codex", "dsh"):
        surf = surfaces.get(harness, "none")
        add(f"surface:{harness}", True, surf)

    add("webhook lane enabled", hermes_mod.webhook_enabled(),
        "" if hermes_mod.webhook_enabled() else "run: galvanize init")
    if hermes_mod.webhook_enabled():
        ok = hermes_mod.gateway_running()
        add("gateway /health", ok, "" if ok else "start: hermes gateway run")
    add("daemon", s["daemon_alive"], f"heartbeat {s['daemon_heartbeat']}")
    serve_live = serve_mod.running_info() is not None
    add("serve api", serve_live,
        f"api v{serve_mod.API_VERSION}" if serve_live else "not running (galvanize serve)")
    add("secrets", True, secrets_mod.migrate_note())

    # folder paths still exist; hermes routes not orphaned
    for t in ts.values():
        if t.source_type == "folder":
            p = Path(str(t.source.get("path", ""))).expanduser()
            add(f"folder '{t.name}'", p.is_dir(), "" if p.is_dir() else f"missing: {p}")
        if t.wake_kind == "hermes" and hermes_mod.webhook_enabled() \
                and not hermes_mod.route_secret(t.name):
            add(f"route '{t.name}'", False,
                "route missing — re-add: galvanize add ... --name " + t.name)
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def _hermes_config_has_mcp(hermes_home: Path) -> bool:
    try:
        p = hermes_home / "config.yaml"
        if not p.exists():
            return False
        import yaml
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return "galvanize" in (cfg.get("mcp_servers") or {})
    except Exception:
        return False
