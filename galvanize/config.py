"""Trigger definitions: the triggers.yaml spine + global config.

One YAML holds all triggers (hot-reloaded by the daemon). Secrets are NOT
stored here — HMAC secrets live in the Hermes subscriptions file that
galvanize manages; anything sensitive belongs in the OS credential store
later (v0.1 keeps the local trust boundary: everything is user-owned files).
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .paths import config_path, ensure_home, triggers_path

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

VALID_SOURCES = {"emit", "folder", "webhook", "imap"}
VALID_WAKE = {"hermes", "shell"}

# --wake sugar: CLI-only aliases that compile down to a shell wake.
# (Claude/Codex/DSH have no inbound lane — waking them IS spawning their
# headless CLI, with the recursion guard injected by dispatch.)
WAKE_PRESETS = {
    "claude": 'claude -p "{prompt}"',
    # workspace-write: "a file landed, do something with it" needs file
    # writes; PLAN §4.1 pre-declares this in config (never auto-elevates).
    "codex": 'codex exec --skip-git-repo-check --sandbox workspace-write "{prompt}"',
    "dsh": 'dsh --profile headless "{prompt}"',
}


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


@dataclass
class Trigger:
    name: str
    source: Dict[str, Any]          # {"type": "folder", "path": ..., ...}
    wake: Dict[str, Any]            # {"kind": "hermes", "deliver": "telegram", ...}
    prompt: str = ""
    cooldown_s: float = 0.0
    dedupe_key: str = ""
    enabled: bool = True
    created_at: str = ""
    description: str = ""

    @property
    def source_type(self) -> str:
        return str(self.source.get("type", ""))

    @property
    def wake_kind(self) -> str:
        return str(self.wake.get("kind", ""))

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "source": self.source,
            "wake": self.wake,
            "enabled": self.enabled,
        }
        if self.description:
            d["description"] = self.description
        if self.prompt:
            d["prompt"] = self.prompt
        if self.cooldown_s:
            d["cooldown_s"] = self.cooldown_s
        if self.dedupe_key:
            d["dedupe_key"] = self.dedupe_key
        if self.created_at:
            d["created_at"] = self.created_at
        return d

    @classmethod
    def from_dict(cls, name: str, d: Dict[str, Any]) -> "Trigger":
        return cls(
            name=name,
            source=d.get("source") or {},
            wake=d.get("wake") or {},
            prompt=str(d.get("prompt", "")),
            cooldown_s=float(d.get("cooldown_s", 0) or 0),
            dedupe_key=str(d.get("dedupe_key", "")),
            enabled=bool(d.get("enabled", True)),
            created_at=str(d.get("created_at", "")),
            description=str(d.get("description", "")),
        )

    def validate(self) -> List[str]:
        errs: List[str] = []
        if not valid_name(self.name):
            errs.append(f"invalid trigger name '{self.name}' (lowercase letters, digits, - _)")
        st = self.source_type
        if st not in VALID_SOURCES:
            errs.append(f"source.type '{st}' not one of {sorted(VALID_SOURCES)}")
        if st == "folder":
            p = self.source.get("path")
            if not p:
                errs.append("folder source needs 'path'")
            elif not Path(str(p)).expanduser().is_dir():
                errs.append(f"folder path does not exist: {p}")
        if st == "imap":
            for req_key in ("host", "user"):
                if not self.source.get(req_key):
                    errs.append(f"imap source needs '{req_key}'")
            if not (self.source.get("password") or self.source.get("secret_key")):
                errs.append("imap source needs a password (stored via secret_key)")
        wk = self.wake_kind
        if wk not in VALID_WAKE:
            errs.append(f"wake.kind '{wk}' not one of {sorted(VALID_WAKE)}")
        if wk == "shell" and not self.wake.get("command"):
            errs.append("shell wake needs 'command'")
        return errs


# ---------------------------------------------------------------- global config

@dataclass
class GlobalConfig:
    hermes_deliver: str = "log"
    hermes_home_override: str = ""
    relay_url: str = ""
    dsh_wake_profile: str = "headless"

    @classmethod
    def load(cls) -> "GlobalConfig":
        p = config_path()
        d: Dict[str, Any] = {}
        if p.exists():
            try:
                d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                d = {}
        return cls(
            hermes_deliver=str(d.get("hermes_deliver", "log")),
            hermes_home_override=str(d.get("hermes_home_override", "")),
            relay_url=str(d.get("relay_url", "") or ""),
            dsh_wake_profile=str(d.get("dsh_wake_profile", "headless") or "headless"),
        )

    def save(self) -> None:
        ensure_home()
        _atomic_write_yaml(
            config_path(),
            {
                "hermes_deliver": self.hermes_deliver,
                "hermes_home_override": self.hermes_home_override or None,
                "relay_url": self.relay_url or None,
                "dsh_wake_profile": self.dsh_wake_profile,
            },
        )


def _atomic_write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- registry

def triggers_path_mtime() -> float:
    p = triggers_path()
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def load_triggers() -> Dict[str, Trigger]:
    p = triggers_path()
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out: Dict[str, Trigger] = {}
    raw = data.get("triggers") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        for name, td in raw.items():
            if isinstance(td, dict):
                out[str(name)] = Trigger.from_dict(str(name), td)
    return out


def save_triggers(triggers: Dict[str, Trigger]) -> None:
    ensure_home()
    payload = {
        "triggers": {name: t.to_dict() for name, t in sorted(triggers.items())}
    }
    _atomic_write_yaml(triggers_path(), payload)


def upsert_trigger(t: Trigger) -> None:
    ts = load_triggers()
    ts[t.name] = t
    save_triggers(ts)


def remove_trigger(name: str) -> bool:
    ts = load_triggers()
    if name not in ts:
        return False
    del ts[name]
    save_triggers(ts)
    return True


def set_enabled(name: str, enabled: bool) -> bool:
    ts = load_triggers()
    t = ts.get(name)
    if t is None:
        return False
    t.enabled = bool(enabled)
    upsert_trigger(t)
    return True


def new_secret() -> str:
    return secrets.token_urlsafe(32)
