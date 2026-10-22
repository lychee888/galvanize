"""Git-hook installer: post-commit/post-merge hooks that emit events.

Two modes:
  hook      — write .git/hooks/{post-commit,post-merge} calling
              `galvanize emit <name> --json {...}` (zero deps, exact events)
  watch     — no repo mutation; a folder watch on <repo>/.git/HEAD +
              refs via the daemon (lower fidelity, offered as opt-in)

Recursion guard: the hook body skips when GALVANIZE_SPAWN=1 is in the
environment (an agent woken by this very hook must not re-fire it).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .. import winlaunch

_HOOK_SH = """#!/bin/sh
# galvanize trigger '{name}' — managed file; re-add with `galvanize add git-hook`
[ "$GALVANIZE_SPAWN" = "1" ] && exit 0
repo="$(git rev-parse --show-toplevel 2>/dev/null)"
sha="$(git rev-parse HEAD 2>/dev/null)"
payload='{{"repo":"'"$repo"'","sha":"'"$sha"'","event_type":"git.{event}"}}'
{emitter} emit {name} --json "$payload" >/dev/null 2>&1
exit 0
"""


def default_emitter() -> str:
    """Absolute, PATH-independent command that runs `galvanize emit`.

    Resolution: the entry-point exe next to this interpreter, else the
    interpreter itself with -m (works in every install shape incl. uvx).
    """
    exe = Path(sys.executable)
    cand = exe.parent / "galvanize.exe"
    target = cand if cand.exists() else exe
    # forward slashes: sh eats backslash escapes, and \ breaks JSON payloads
    text = str(target).replace("\\", "/")
    if " " in text:
        text = f'"{text}"'
    if cand.exists():
        return text
    if exe.name.lower() in ("python.exe", "pythonw.exe"):
        return f"{text} -m galvanize.cli"
    return "galvanize"


def repo_root(path: str) -> Optional[Path]:
    try:
        r = winlaunch.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                          timeout=10)
        if r.returncode == 0:
            return Path(r.stdout.strip())
    except Exception:
        pass
    p = Path(path).expanduser()
    return p if (p / ".git").is_dir() else None


def install_hooks(repo_path: str, trigger_name: str,
                  events: tuple = ("commit", "merge"),
                  emitter: Optional[str] = None) -> dict:
    root = repo_root(repo_path)
    if root is None:
        return {"ok": False, "error": f"not a git repository: {repo_path}"}
    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        return {"ok": False, "error": f"no .git/hooks dir in {root}"}
    emitter = emitter or default_emitter()
    written = []
    for ev in events:
        hook = hooks_dir / f"post-{ev}"
        if hook.exists() and "galvanize" not in hook.read_text(errors="ignore"):
            return {"ok": False, "error":
                    f"pre-existing post-{ev} hook we don't own — refusing to overwrite: {hook}"}
        hook.write_text(_HOOK_SH.format(name=trigger_name, event=ev, emitter=emitter),
                        encoding="utf-8", newline="\n")
        try:
            hook.chmod(0o755)
        except OSError:
            pass
        written.append(str(hook))
    return {"ok": True, "root": str(root), "hooks": written}


def uninstall_hooks(repo_path: str, events: tuple = ("commit", "merge")) -> dict:
    root = repo_root(repo_path)
    if root is None:
        return {"ok": False, "error": f"not a git repository: {repo_path}"}
    removed = []
    for ev in events:
        hook = root / ".git" / "hooks" / f"post-{ev}"
        if hook.exists() and "galvanize" in hook.read_text(errors="ignore"):
            hook.unlink()
            removed.append(str(hook))
    return {"ok": True, "removed": removed}
