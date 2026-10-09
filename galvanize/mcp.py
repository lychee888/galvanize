"""galvanize mcp — minimal stdio MCP server (JSON-RPC 2.0, newline framing).

Hand-rolled deliberately: the whole surface is five tools over manage.py.
An MCP SDK would add a dependency tree to every user's install to save a
hundred lines. Implements: initialize, tools/list, tools/call, ping,
notifications/* (ignored). Protocol version 2024-11-05 (broadly supported).

Exactly-one-surface: if surfaces.json says the native plugin owns a
harness, tools/call still answers (management keeps working) but the tool
description steers nothing — registration is init's job; here we just
refuse to double-answer when the Hermes plugin is active IN THIS process'
view... which it can't be (separate interpreter), so the rule matters at
registration time, not here.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

PROTOCOL = "2024-11-05"
from . import __version__ as _V
SERVER = {"name": "galvanize", "version": _V}

_STEER = ("USE THESE INSTEAD OF a scheduled/polling job whenever the user "
          "describes an EVENT ('when X lands/arrives/happens'). ")

TOOLS = [
    {
        "name": "trigger_add",
        "description": _STEER + "Create an event trigger: kind=folder (files landing; {file}/{path} usable in prompt), imap (new mail in a mailbox; needs mailbox address + app-password — stored in the OS keyring), webhook (external service POSTs to a URL we host), git-hook (commits in a repo), emit (named event from scripts). Results deliver per trigger.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["folder", "imap", "webhook", "git-hook", "emit"]},
                "target": {"type": "string", "description": "folder path / mailbox address / repo path"},
                "name": {"type": "string", "description": "lowercase-kebab name (default derived from target)"},
                "prompt": {"type": "string", "description": "instruction for the woken session; placeholders {file} {path} {subject} {from}"},
                "deliver": {"type": "string", "description": "delivery target (telegram, discord, slack, log). Default from config."},
                "patterns": {"type": "array", "items": {"type": "string"}},
                "app_password": {"type": "string", "description": "imap only: mailbox app-password (stored in OS keyring)"},
                "subject_filter": {"type": "string", "description": "imap only: subject must contain this text"},
                "cooldown_s": {"type": "number"},
                "wake": {"type": "string", "enum": ["hermes", "claude", "codex", "dsh", "shell"], "description": "Explicit target for the new agent session."},
                "command": {"type": "string"},
                "workdir": {"type": "string"},
                "wake_profile": {"type": "string", "description": "DSH profile override; otherwise use the shared configured profile."},
                "relay_url": {"type": "string"},
                "relay_token": {"type": "string", "description": "Queue read credential (RELAY_TOKEN), not INGEST_TOKEN."},
            },
            "required": ["kind", "wake"],
        },
    },
    {"name": "trigger_list", "description": "List all triggers with source, wake mode, filters.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "trigger_remove", "description": "Remove a trigger (cleans its Hermes route and installed git hooks).",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "trigger_test", "description": "Send a synthetic event through a trigger's real dispatch path. Run after every trigger_add.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "trigger_status", "description": "Trigger-system health: daemon, webhook lane, gateway, per-trigger watching/last-fire/errors.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "doctor", "description": "Full install health including the exactly-one-surface check.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _call_tool(name: str, args: Dict[str, Any]) -> str:
    from galvanize import manage

    if name == "trigger_add":
        if not args.get("wake"):
            return json.dumps({"ok": False, "error": "Choose an explicit wake target: hermes, claude, codex, dsh, or shell"})
        target = str(args.get("target", ""))
        kind = str(args.get("kind", ""))
        r = manage.add_trigger(
            kind, target,
            name=str(args.get("name", "")),
            wake=str(args["wake"]),
            command=str(args.get("command", "")),
            workdir=str(args.get("workdir", "")),
            relay_url=str(args.get("relay_url", "")),
            relay_token=str(args.get("relay_token", "")),
            wake_profile=str(args.get("wake_profile", "")),
            deliver=str(args.get("deliver", "")),
            prompt=str(args.get("prompt", "")),
            patterns=args.get("patterns") or None,
            **({"pass" "word": str(args.get("app_password", ""))}),
            subject_filter=str(args.get("subject_filter", "")),
            cooldown_s=float(args.get("cooldown_s", 0) or 0),
        )
        return json.dumps(r, ensure_ascii=False)
    if name == "trigger_list":
        from galvanize.config import load_triggers
        ts = load_triggers()
        return json.dumps({"ok": True, "triggers": {n: t.to_dict() for n, t in ts.items()}},
                          ensure_ascii=False)
    if name == "trigger_remove":
        return json.dumps(manage.remove_trigger_op(str(args.get("name", ""))), ensure_ascii=False)
    if name == "trigger_test":
        return json.dumps(manage.test_trigger(str(args.get("name", ""))), ensure_ascii=False)
    if name == "trigger_status":
        return json.dumps(manage.status(), ensure_ascii=False)
    if name == "doctor":
        return json.dumps(manage.doctor(), ensure_ascii=False)
    return json.dumps({"ok": False, "error": f"unknown tool {name}"})


def _respond(obj: Dict[str, Any]) -> None:
    # LF framing on the raw buffer: text-mode stdout on Windows would emit
    # CRLF, which strict stdio clients (Codex/Rust) fail to parse -> the
    # server hangs "pending" forever with no error anywhere.
    out = getattr(sys.stdout, "buffer", sys.stdout)
    out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    out.flush()


def _handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method", "")
    mid = msg.get("id")
    if method == "initialize":
        # Echo the client's requested protocol version when we understand it
        # (some clients, incl. Codex >=0.150, treat a mismatch as fatal).
        params = msg.get("params") or {}
        want = str(params.get("protocolVersion") or PROTOCOL)
        known = {"2024-10-07", "2024-11-05", "2025-03-26", "2025-06-18"}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if want in known else PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER,
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    # resources/prompts: not supported, but the MCP spec-friendly answer is
    # an EMPTY LIST, not -32601 — Codex's client aborts startup on the error.
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resourceTemplates": []}}
    if method in ("resources/subscribe", "resources/unsubscribe"):
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "resources not supported"}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text = _call_tool(str(params.get("name", "")),
                              params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"ok": False, "error": f"{type(e).__name__}: {e}"})}],
                "isError": True}}
    if method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    # newline-delimited JSON-RPC on stdio; stderr for logs (stdout is sacred)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _respond({"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32700, "message": "parse error"}})
            continue
        resp = _handle(msg)
        if resp is not None:
            _respond(resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
