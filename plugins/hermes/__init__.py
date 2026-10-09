"""Galvanize Hermes plugin — trigger management tools for the agent.

Why this exists: the agent's tool list shapes its habits. cronjob sits in
schema every turn, so "watch for X" becomes an hourly poller. These tools
give event-driven activation the same prominence. Wake-up itself stays
external (the galvanize daemon / the Hermes webhook lane); this plugin is
the management surface only.

Tool handlers try `import galvanize` (same interpreter when galvanize is
installed into Hermes' venv) and fall back to the `galvanize` CLI on PATH,
so the plugin degrades gracefully when the daemon lives elsewhere.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


def _run_op(op: str, **kwargs):
    """Call a galvanize manage op, in-process if possible, CLI otherwise."""
    try:
        from galvanize import manage

        fn = {
            "add": manage.add_trigger,
            "remove": manage.remove_trigger_op,
            "test": manage.test_trigger,
            "status": manage.status,
            "list": None,
        }[op]
        if op == "list":
            from galvanize.config import load_triggers

            ts = load_triggers()
            return {"ok": True, "triggers": {n: t.to_dict() for n, t in ts.items()}}
        return fn(**kwargs)
    except ImportError:
        if not shutil.which("galvanize"):
            return {"ok": False, "error":
                    "galvanize is not installed in this interpreter and 'galvanize' "
                    "is not on PATH. Fix:  <hermes-python> -m pip install galvanize"
                    "   (or run: galvanize init)"}
        cmd = ["galvanize"]
        cmd += {"add": ["add"], "remove": ["remove"], "test": ["test"],
                "status": ["status"], "list": ["status"]}[op]
        # CLI fallback: reconstruct the arg vector so trigger_add still works
        # when the package isn't importable in the gateway interpreter.
        if op == "add":
            kind = kwargs.get("kind", "")
            target = kwargs.get("target", "")
            if not kind:
                return {"ok": False, "error": "trigger_add needs 'kind'"}
            cmd += [kind] + ([target] if target else [])
            for flag, key in (("--name", "name"), ("--wake", "wake"),
                              ("--deliver", "deliver"), ("--prompt", "prompt"),
                              ("--workdir", "workdir"), ("--command", "command")):
                val = kwargs.get(key, "")
                if val:
                    cmd += [flag, str(val)]
            if kwargs.get("patterns"):
                cmd += ["--patterns", ",".join(kwargs["patterns"])]
            if kwargs.get("events"):
                cmd += ["--events", ",".join(kwargs["events"])]
            if kwargs.get("cooldown_s"):
                cmd += ["--cooldown", str(kwargs["cooldown_s"])]
        elif op == "remove":
            cmd += [kwargs.get("name", "")]
        elif op == "test":
            cmd += [kwargs.get("name", "")]
            if kwargs.get("payload"):
                cmd += ["--payload", json.dumps(kwargs["payload"])]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                  encoding="utf-8", errors="replace")
            return {"ok": proc.returncode == 0,
                    "output": (proc.stdout or proc.stderr)[-3000:]}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- schemas

ADD_SCHEMA = {
    "name": "trigger_add",
    "description": (
        "Create an event trigger that wakes a fresh Hermes session when something "
        "happens. USE THIS INSTEAD OF a scheduled/polling cron job whenever the user "
        "describes an EVENT: 'wake me when X lands/arrives/happens', 'notify me when "
        "a file shows up', 'when GitHub pings me...'. Sources: "
        "folder (files landing in a directory — {file} and {path} usable in prompt), "
        "webhook (external service POSTs to a URL we host: GitHub, Stripe, monitoring), "
        "emit (named event fired by scripts or other agents via `galvanize emit`). "
        "Results deliver per-trigger (deliver='telegram'|'discord'|...|'log')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["folder", "webhook", "emit"]},
            "name": {"type": "string", "description": "lowercase-kebab trigger name"},
            "target": {"type": "string", "description": "folder path to watch (kind=folder)"},
            "prompt": {"type": "string", "description": "instruction for the woken session; placeholders {file} {path} or webhook payload fields like {pull_request.title}"},
            "patterns": {"type": "array", "items": {"type": "string"}, "description": "filename globs, e.g. ['*.step']"},
            "deliver": {"type": "string", "description": "delivery target for results (telegram, discord, slack, log)"},
            "events": {"type": "array", "items": {"type": "string"}, "description": "kind=webhook: event types to accept"},
            "cooldown_s": {"type": "number", "description": "minimum seconds between wakes (default 0)"},
        },
        "required": ["kind", "name"],
    },
}

LIST_SCHEMA = {
    "name": "trigger_list",
    "description": "List all galvanize triggers with their source, wake mode, and settings.",
    "parameters": {"type": "object", "properties": {}},
}

REMOVE_SCHEMA = {
    "name": "trigger_remove",
    "description": "Remove a galvanize trigger (and its Hermes webhook route if it had one).",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
}

TEST_SCHEMA = {
    "name": "trigger_test",
    "description": (
        "Inject a synthetic event through a trigger's real dispatch path and report "
        "what happened. ALWAYS run this right after trigger_add so the user sees the "
        "trigger work before relying on it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "payload": {"type": "object", "description": "optional synthetic payload"},
        },
        "required": ["name"],
    },
}

STATUS_SCHEMA = {
    "name": "trigger_status",
    "description": (
        "Health of the trigger system: daemon alive?, Hermes webhook lane enabled and "
        "reachable?, per-trigger last-fire time, fires today, last error. Use to answer "
        "'is my trigger still watching?' honestly instead of guessing."
    ),
    "parameters": {"type": "object", "properties": {}},
}


# ---------------------------------------------------------------- handlers

def _handle_trigger_add(args, **kw):
    r = _run_op(
        "add",
        args.get("kind", ""),
        args.get("target", "") or str(args.get("target_path", "")),
        name=args.get("name", ""),
        wake="hermes",
        deliver=args.get("deliver", "") or "",
        prompt=args.get("prompt", "") or "",
        patterns=args.get("patterns") or None,
        events=args.get("events") or None,
        cooldown_s=float(args.get("cooldown_s", 0) or 0),
    )
    return json.dumps(r, ensure_ascii=False)


def _handle_trigger_list(args, **kw):
    return json.dumps(_run_op("list"), ensure_ascii=False)


def _handle_trigger_remove(args, **kw):
    return json.dumps(_run_op("remove", name=args.get("name", "")), ensure_ascii=False)


def _handle_trigger_test(args, **kw):
    return json.dumps(
        _run_op("test", name=args.get("name", ""), payload=args.get("payload") or None),
        ensure_ascii=False,
    )


def _handle_trigger_status(args, **kw):
    return json.dumps(_run_op("status"), ensure_ascii=False)


# ---------------------------------------------------------------- slash + CLI

def _slash_triggers(raw_args: str) -> str:
    """/triggers [list|status|test <name>] — zero-LLM trigger checks."""
    parts = (raw_args or "").split()
    verb = parts[0].lower() if parts else "list"
    try:
        from galvanize import manage
    except ImportError:
        import sys
        return ("galvanize is not importable in this interpreter. "
                f"Fix: {sys.executable} -m pip install galvanize "
                "(or run: galvanize init)")
    if verb == "test" and len(parts) > 1:
        r = manage.test_trigger(parts[1])
        return ("✔ " + r.get("detail", "accepted")) if r.get("ok") else ("✖ " + r.get("error", "failed"))
    s = manage.status()
    lines = [f"daemon: {'up' if s['daemon_alive'] else 'DOWN'}  "
             f"webhook lane: {'on' if s['webhook_enabled'] else 'off'}  "
             f"gateway: {'up' if s['gateway_running'] else 'down'}", ""]
    if not s["triggers"]:
        lines.append("No triggers yet — describe the event you want watched.")
    for row in s["triggers"]:
        mark = ("●" if row["watching"] else "○" if row["enabled"] is False
                else "⇢" if row.get("manual") else "–")
        err = f"  ERR: {str(row['last_error'])[:40]}" if row["last_error"] else ""
        lines.append(f"{mark} {row['name']}  {row['source']}→{row['wake']}  "
                     f"last: {row['last_fire']}  today: {row['fires_today']}{err}")
    for n in s["notes"]:
        lines.append(f"! {n}")
    return "\n".join(lines)


def _cli_setup_triggers(subparser) -> None:
    subparser.add_argument("verb", nargs="?", default="status",
                           choices=["status", "list", "test", "doctor"],
                           help="what to show (default: status)")
    subparser.add_argument("name", nargs="?", default="",
                           help="trigger name (for test)")


def _cli_handle_triggers(args) -> None:
    """`hermes triggers ...` — thin wrapper over manage ops.

    Falls back to the `galvanize` CLI shim when the package isn't importable
    in THIS interpreter: the hermes launcher runs the CLI with `-I -S`, which
    drops site-packages from sys.path, so the verb would otherwise die even
    on a perfectly installed machine.
    """
    try:
        from galvanize import manage
    except ImportError:
        import shutil
        import subprocess
        import sys
        verb0 = getattr(args, "verb", "status")
        shim = shutil.which("galvanize") or shutil.which("galvanize.exe")
        if not shim:
            print("galvanize not importable in this interpreter and the "
                  "'galvanize' CLI is not on PATH.\n"
                  f"  Fix: {sys.executable} -m pip install galvanize   "
                  "(or run: galvanize init)")
            raise SystemExit(1)
        argv = [shim, "doctor" if verb0 == "doctor" else "status"]
        if verb0 == "test":
            argv = [shim, "test", getattr(args, "name", "") or ""]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                               encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"galvanize CLI fallback failed: {e}")
            raise SystemExit(1)
        print((p.stdout or p.stderr).rstrip())
        raise SystemExit(p.returncode)
    verb = getattr(args, "verb", "status")
    if verb == "test":
        r = manage.test_trigger(getattr(args, "name", "") or "")
        print(r.get("detail") or r.get("error") or "done") if (r.get("ok") or r.get("error")) else print("done")
        for ln in r.get("lines", []):
            print(" ", ln)
        return
    if verb == "doctor":
        r = manage.doctor()
        for c in r["checks"]:
            print(("✔ " if c["ok"] else "✖ ") + f"{c['name']:<28} {c['note']}")
        return
    s = manage.status() if verb in ("status", "list") else manage.status()
    if not s["triggers"]:
        print("No triggers yet — ask the agent to create one, or: galvanize add folder <path>")
    for row in s["triggers"]:
        mark = ("●" if row["watching"] else "⇢" if row.get("manual") else "–")
        print(f"{mark} {row['name']:<24} "
              f"{row['source']:>8} → {row['wake']:<6} last: {row['last_fire']}  "
              f"today: {row['fires_today']}")
    for n in s["notes"]:
        print(f"! {n}")


def _galvanize_present() -> bool:
    try:
        import galvanize  # noqa: F401
        return True
    except ImportError:
        return bool(shutil.which("galvanize"))


def register(ctx):
    for schema, handler in (
        (ADD_SCHEMA, _handle_trigger_add),
        (LIST_SCHEMA, _handle_trigger_list),
        (REMOVE_SCHEMA, _handle_trigger_remove),
        (TEST_SCHEMA, _handle_trigger_test),
        (STATUS_SCHEMA, _handle_trigger_status),
    ):
        ctx.register_tool(
            name=schema["name"],
            toolset="galvanize",
            schema=schema,
            handler=handler,
            description=schema["description"],
            check_fn=_galvanize_present,
        )

    # Zero-LLM management surfaces (PLAN item 12b/12c): /triggers slash in
    # chat sessions + `hermes triggers` terminal verb.
    try:
        ctx.register_command(
            name="triggers",
            handler=_slash_triggers,
            description="List galvanize triggers and their health (no agent turn spent)",
            args_hint="[list|status|test <name>]",
        )
    except Exception as e:  # older hosts without register_command: tool surface still works
        logger.debug("galvanize: /triggers slash not registered: %s", e)
    try:
        ctx.register_cli_command(
            name="triggers",
            help="show or test galvanize event triggers",
            setup_fn=_cli_setup_triggers,
            handler_fn=_cli_handle_triggers,
            description="Thin wrapper over galvanize manage ops (status/list/test/doctor).",
        )
    except Exception as e:
        logger.debug("galvanize: hermes triggers CLI not registered: %s", e)
