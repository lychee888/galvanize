"""Trigger definitions: SQLite authority + the triggers.yaml import/export mirror.

Since card C5 (audit F8/F9) the writable authority for trigger config is
galvanize.db (see store.py): every editor — CLI, Hermes plugin, serve API,
dashboard — goes through the same transactional repository with revision
checks, offline, no daemon IPC in the consistency path. triggers.yaml is
demoted to (a) the first-seed source on migration and (b) an import/export
mirror that is rewritten after every activated change so humans can still
read and version it; hand-edits to the YAML no longer hot-reload until you
run `galvanize config import`. A corrupt import NEVER clobbers: the last
committed revision keeps serving and the doctor row goes loud.

Secrets are NOT stored in either artifact; anything sensitive lives in the
OS credential store (secrets.py).
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import store
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
#
# The registry reads/writes the SQLite authority (store.py). `ensure_ready`
# makes the one-time migration from the pre-C5 JSON files happen lazily at
# whichever entry point touches the store first — supervisor startup, the
# daemon, or a bare CLI call on a machine where nothing has run yet.

def _boot() -> None:
    store.ensure_ready()


def config_revision() -> int:
    """Current activated config revision (daemon hot-reload poll target)."""
    _boot()
    return store.current_revision()


def parse_triggers_yaml(text: str) -> Tuple[Dict[str, dict], List[str]]:
    """Validate a YAML document into {name: body} without touching the db.

    Returns (bodies, hard_errors). All-or-nothing: any hard error voids the
    whole document (a partially-activated config would silently stop
    watchers whose neighbours failed). A folder path that does not exist
    right now is NOT a hard error — the doctor has a dedicated per-trigger
    row for that.
    """
    data = yaml.safe_load(text)
    raw = data.get("triggers") if isinstance(data, dict) else None
    if raw is None:
        return {}, ["document has no 'triggers:' mapping"]
    if not isinstance(raw, dict):
        return {}, ["'triggers:' is not a mapping"]
    bodies: Dict[str, dict] = {}
    hard: List[str] = []
    for name, td in raw.items():
        name = str(name)
        if not isinstance(td, dict):
            hard.append(f"{name}: not a mapping")
            continue
        t = Trigger.from_dict(name, td)
        errs = [e for e in t.validate() if "folder path does not exist" not in e]
        if errs:
            hard.append(f"{name}: {'; '.join(errs)}")
            continue
        bodies[name] = t.to_dict()
    return bodies, hard


def load_triggers() -> Dict[str, Trigger]:
    _boot()
    return {name: Trigger.from_dict(name, body)
            for name, body in store.config_bodies().items()}


def export_yaml(path: Optional[Path] = None) -> Path:
    """Rewrite the triggers.yaml mirror from the authority (never the source)."""
    _boot()
    p = path or triggers_path()
    payload = {"triggers": {name: body for name, body in
                            sorted(store.config_bodies().items())}}
    _atomic_write_yaml(p, payload)
    return p


def import_yaml(path: Path, *, dry_run: bool = False,
                expected_revision: Optional[int] = None) -> Dict[str, Any]:
    """Activate a YAML file as a new config revision, or fail loudly.

    Corruption/invalidity keeps the committed last-known-good serving (the
    watcher set does not change), records a loud store config_state, and
    returns ok=False — the doctor row explains it until the next successful
    activation.
    """
    _boot()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        store.note_config_state("corrupt", f"{path}: {e}")
        return {"ok": False, "error": f"cannot read {path}: {e}"}
    try:
        bodies, hard = parse_triggers_yaml(text)
    except yaml.YAMLError as e:
        store.note_config_state("corrupt", f"unparseable YAML: {e}")
        return {"ok": False, "error": f"unparseable YAML: {e}"}
    if hard:
        store.note_config_state("corrupt", "; ".join(hard)[:500])
        return {"ok": False, "error": "; ".join(hard), "invalid": hard}
    cur = store.current_revision()
    if dry_run:
        return {"ok": True, "dry_run": True, "revision": cur,
                "triggers": sorted(bodies),
                "changes": len(set(bodies) ^ set(store.config_bodies().keys()))}
    try:
        rev = store.replace_config(bodies, expected_revision=expected_revision,
                                   note=f"import {Path(path).name}")
    except store.StaleRevisionError as e:
        return {"ok": False, "conflict": True, "error": str(e)}
    export_yaml()
    return {"ok": True, "revision": rev, "triggers": sorted(bodies)}


def save_triggers(triggers: Dict[str, Trigger], *,
                  expected_revision: Optional[int] = None) -> int:
    """Full-set write through the authority (revision-checked)."""
    _boot()
    rev = store.replace_config(
        {name: t.to_dict() for name, t in triggers.items()},
        expected_revision=expected_revision, note="save_triggers")
    export_yaml()
    return rev


def upsert_trigger(t: Trigger, *,
                   expected_revision: Optional[int] = None) -> int:
    _boot()
    rev = store.upsert_body(t.name, t.to_dict(),
                            expected_revision=expected_revision,
                            note=f"upsert {t.name}")
    export_yaml()
    return rev


def remove_trigger(name: str, *,
                   expected_revision: Optional[int] = None) -> bool:
    _boot()
    if name not in store.config_bodies():
        return False
    store.delete_body(name, expected_revision=expected_revision,
                      note=f"remove {name}")
    export_yaml()
    return True


def set_enabled(name: str, enabled: bool, *,
                expected_revision: Optional[int] = None) -> bool:
    """Flip one trigger's enabled flag inside ONE read-modify-write tx."""
    _boot()
    found = {"hit": False}

    def _fn(bodies: Dict[str, dict]) -> None:
        body = bodies.get(name)
        if body is None:
            return
        found["hit"] = True
        body["enabled"] = bool(enabled)

    try:
        rev = store.mutate_config(_fn, expected_revision=expected_revision,
                                  note=f"set_enabled {name}")
    except store.StaleRevisionError:
        raise
    if found["hit"]:
        export_yaml()
    return bool(found["hit"]) and rev > 0  # legacy bool contract


def new_secret() -> str:
    return secrets.token_urlsafe(32)
