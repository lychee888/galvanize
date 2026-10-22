"""Dispatchers: translate an event into "start a fresh agent session".

wake.kind == "hermes":  POST into the Hermes webhook lane (HMAC V2).
wake.kind == "shell":   run a command template ({prompt}/{payload}
                        placeholders, GALVANIZE_* env) — the escape hatch
                        that covers claude -p / codex exec / anything else.

Recursion guard: every spawn carries GALVANIZE_SPAWN=1 so scripts and
hooks can refuse to re-trigger themselves.
"""

from __future__ import annotations

import json
import os
import subprocess
import shlex
import shutil
import re
from typing import Tuple

from . import hermes as hermes_mod
from . import state, winlaunch
from .paths import ensure_home
from .events import Event

# F12: error details carry a generous tail; the FULL output lands in
# <state dir>/wake_output/<trigger>.log (one file per trigger, overwritten
# each wake, so nothing accumulates).
DETAIL_TAIL = 2000


def _dump_wake_output(name: str, command, status: str, stdout: str, stderr: str) -> str:
    try:
        d = ensure_home() / "wake_output"
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]", "_", name) or "wake"
        p = d / f"{safe}.log"
        p.write_text(
            f"{status} | cmd={command!r}\n\n=== stdout ===\n{stdout}\n"
            f"\n=== stderr ===\n{stderr}\n",
            encoding="utf-8", errors="replace")
        return str(p)
    except Exception:
        # The dump is diagnostic only: the wake result is already decided,
        # and a weird runtime (e.g. monkeypatched os.name in tests) must
        # never sink an otherwise-successful dispatch.
        return "(unavailable)"


def dispatch(trigger, event: Event, prompt: str) -> Tuple[bool, str]:
    """Dispatch one event for *trigger*. Returns (ok, detail)."""
    kind = trigger.wake_kind
    try:
        if kind == "hermes":
            ok, detail = _wake_hermes(trigger, event, prompt)
        elif kind == "shell":
            ok, detail = _wake_shell(trigger, event, prompt)
        else:
            ok, detail = False, f"unknown wake.kind '{kind}'"
    except hermes_mod.HermesNotConfigured as e:
        ok, detail = False, str(e)
    except Exception as e:  # dispatcher must never crash the bus
        ok, detail = False, f"{type(e).__name__}: {e}"
    state.record_fire(trigger.name, ok=ok, detail=detail)
    return ok, detail


def _wake_hermes(trigger, event: Event, prompt: str) -> Tuple[bool, str]:
    status, resp = hermes_mod.post_event(
        trigger.name,
        event,
        prompt=prompt,
        secret=trigger.wake.get("secret") or None,
    )
    if status in (200, 202):
        sess = resp.get("delivery_id") or resp.get("status", "ok")
        return True, f"hermes session spawned ({resp.get('status', '?')}, id {sess})"
    return False, f"hermes POST {status}: {json.dumps(resp)[:200]}"


def _command_args(command: str) -> list[str]:
    """Parse trusted command syntax before adding any event-controlled data."""
    if os.name != "nt":
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        args = list(lexer)
    else:
        # Use Windows' native quoting rules; POSIX shlex eats backslashes.
        import ctypes
        from ctypes import wintypes
        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        parse.restype = ctypes.POINTER(wintypes.LPWSTR)
        count = ctypes.c_int()
        result = parse(command, ctypes.byref(count))
        if not result:
            raise ValueError("Could not parse wake command")
        try:
            args = [result[i] for i in range(count.value)]
        finally:
            free = ctypes.windll.kernel32.LocalFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = ctypes.c_void_p
            free(result)
    if not args or any(re.fullmatch(r"[|&;<>()]+", a) for a in args):
        raise ValueError("Commands with placeholders must be a single executable; use a script for shell operators")
    return args


def _resolve_executable(args: list[str]) -> list[str]:
    """Invoke npm's JavaScript entry directly instead of a cmd.exe shim."""
    if os.name != "nt":
        return args
    from pathlib import Path
    exe = shutil.which(args[0]) or args[0]
    path = Path(exe)
    if path.suffix.lower() in (".cmd", ".bat"):
        content = path.read_text(encoding="utf-8")
        match = re.search(r'%dp0%[\\/]([^"\r\n]+\.(?:js|cjs|mjs))', content, re.I)
        if not match:
            raise ValueError("Batch-file wakes with placeholders are unsafe; use an executable or script interpreter directly")
        script = path.parent / match[1].replace("\\", "/")
        node = path.parent / "node.exe"
        node_exe = str(node) if node.is_file() else shutil.which("node")
        if not script.is_file() or not node_exe:
            raise ValueError("Cannot resolve npm wake entry point; configure node and the script path directly")
        return [node_exe, str(script), *args[1:]]
    return [exe, *args[1:]]


def _wake_shell(trigger, event: Event, prompt: str) -> Tuple[bool, str]:
    command = str(trigger.wake.get("command", ""))
    body = json.dumps(event.to_body(), ensure_ascii=False)
    env = dict(os.environ)
    env["GALVANIZE_SPAWN"] = "1"
    env["GALVANIZE_TRIGGER"] = trigger.name
    env["GALVANIZE_PROMPT"] = prompt
    env["GALVANIZE_PAYLOAD"] = body
    templated = "{prompt}" in command or "{payload}" in command
    if templated:
        args = _command_args(command)
        # Resolve the executable before substitution: event text is data only.
        if "{prompt}" in args[0] or "{payload}" in args[0]:
            return False, "The wake executable cannot contain event placeholders"
        args = _resolve_executable(args)
        rendered = [re.sub(r"\{(prompt|payload)\}", lambda m: prompt if m[1] == "prompt" else body, a) for a in args]
    else:
        rendered = command
    timeout = float(trigger.wake.get("timeout_s", 300) or 300)
    cwd = str(trigger.wake.get("workdir", "") or "") or None
    try:
        proc = winlaunch.run(
            rendered, env=env, timeout=timeout, cwd=cwd, shell=not templated,
        )
    except subprocess.TimeoutExpired as exc:
        # The launch primitive terminated the WHOLE tree via the job
        # (audit F12): no cmd.exe orphan can keep a window alive.
        path = _dump_wake_output(trigger.name, command, f"TIMEOUT after {timeout}s",
                                 str(exc.output or ""), str(exc.stderr or ""))
        return False, (f"shell wake timed out after {timeout}s; process tree killed "
                       f"(partial output: {path})")
    out = proc.stdout or ""
    err = proc.stderr or ""
    path = _dump_wake_output(trigger.name, command, f"exit {proc.returncode}", out, err)
    if proc.returncode == 0:
        return True, f"exit 0: {out[-DETAIL_TAIL:].strip() or 'ok'}"
    return False, (f"exit {proc.returncode}: {(err or out)[-DETAIL_TAIL:].strip()}"
                   f" [full output: {path}]")
