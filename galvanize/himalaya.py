"""Read-only bootstrap of mailbox secrets from himalaya's config.

Why: hermes users already keep mail credentials working in himalaya;
`galvanize secrets import-himalaya` copies the IMAP passwords into the
galvanize keyring namespace so nobody types them twice. We never write
to the himalaya config, and no plaintext password ever appears in the
returned lines or check notes.

Key convention lives in manage.add_trigger (``f"imap:{user}"``); secret_key()
below mirrors it deliberately — imported values must land under the SAME
keys existing imap triggers reference, or the import is useless.
"""

from __future__ import annotations

import os

try:
    import tomllib                      # 3.11+ stdlib
except ModuleNotFoundError:             # FIX-CI A: keep the shipped 3.10 floor
    import tomli as tomllib             # type: ignore[import-not-found] (py<3.11 only)

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

ENV_CONFIG = "GALVANIZE_HIMALAYA_CONFIG"


@dataclass
class HimalayaAccount:
    name: str          # himalaya account block name
    email: str         # accounts.<name>.email
    host: str          # imap server hostname
    port: int          # imap server port
    user: str          # sasl username (falls back to email)
    password: Optional[str]  # plaintext app password; None if cmd-store or absent


def secret_key(user: str) -> str:
    """Mirror of manage.add_trigger's key format for imap secrets."""
    return f"imap:{user}"


def config_path(explicit: str = "") -> Optional[Path]:
    """Locate the himalaya config, first match wins:

    1. explicit --config argument (must exist)
    2. $GALVANIZE_HIMALAYA_CONFIG — set: exact path; empty string: DISABLED
    3. $XDG_CONFIG_HOME/himalaya/config.toml
    4. ~/.config/himalaya/config.toml
    5. %APPDATA%/himalaya/config.toml
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    if ENV_CONFIG in os.environ:
        v = os.environ[ENV_CONFIG].strip()
        if not v:
            return None  # explicitly disabled (tests; air-gapped boxes)
        p = Path(v)
        return p if p.is_file() else None
    candidates: List[Path] = []
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        candidates.append(Path(xdg) / "himalaya" / "config.toml")
    candidates.append(Path.home() / ".config" / "himalaya" / "config.toml")
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(Path(appdata) / "himalaya" / "config.toml")
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def _split_server(server: Any) -> Tuple[str, int]:
    s = str(server or "").strip()
    if not s:
        return "", 993
    if "://" in s:
        parts = urlsplit(s)
        default = 993 if (parts.scheme or "").lower() == "imaps" else 143
        return parts.hostname or "", int(parts.port or default)
    return s, 993


def _plaintext_password(pw: Any) -> Optional[str]:
    if isinstance(pw, str) and pw:
        return pw
    if isinstance(pw, dict):
        raw = pw.get("raw")
        if isinstance(raw, str) and raw:
            return raw
    return None  # cmd/eval-sourced or absent: never executed from here


def parse_accounts(path: Path) -> List[HimalayaAccount]:
    """Extract (host, user, plaintext-password) per imap-capable account.

    SMTP-only accounts (no [accounts.x.imap] block) are skipped entirely;
    accounts whose password is cmd-sourced are kept with password=None so
    the drift check can report them as 'no plaintext (skipped)'.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: List[HimalayaAccount] = []
    for name, blk in (data.get("accounts") or {}).items():
        if not isinstance(blk, dict):
            continue
        imap = blk.get("imap")
        if not isinstance(imap, dict):
            continue
        host, port = _split_server(imap.get("server", ""))
        if not host:
            continue
        email = str(blk.get("email") or "")
        user, password = email, None
        sasl = imap.get("sasl")
        if isinstance(sasl, dict):
            plain = sasl.get("plain")
            if isinstance(plain, dict):
                user = str(plain.get("username") or plain.get("user") or email)
                password = _plaintext_password(plain.get("password"))
        if not user:
            continue
        out.append(HimalayaAccount(name=str(name), email=email, host=host,
                                   port=port, user=user, password=password))
    return out


def trigger_names_for_key(key: str) -> List[str]:
    """Existing triggers whose source.secret_key references this key."""
    from .manage import load_triggers
    return sorted(n for n, t in load_triggers().items()
                  if t.source.get("secret_key") == key)


def _do_import(key: str, password: str) -> None:
    from . import secrets as secrets_mod
    secrets_mod.set_secret(key, password)
    secrets_mod.index_keyring_key(key)


def import_himalaya(*, dry_run: bool = False, overwrite: bool = False,
                    config: str = "") -> Dict[str, Any]:
    """Import plaintext imap passwords into the galvanize secret store.

    Returns {ok, config, backend, dry_run, items, lines} — lines are
    display-safe: key names and actions, never password values.
    """
    path = config_path(config)
    if path is None:
        return {"ok": False, "error":
                f"no himalaya config found (tried --config, ${ENV_CONFIG}, "
                "~/.config/himalaya/config.toml, %APPDATA%/himalaya/config.toml)",
                "lines": [], "items": []}
    try:
        accounts = parse_accounts(path)
    except Exception as e:
        return {"ok": False, "error": f"cannot parse {path}: {e}",
                "lines": [], "items": []}
    from . import secrets as secrets_mod
    items: List[Dict[str, Any]] = []
    lines: List[str] = []
    for a in accounts:
        key = secret_key(a.user)
        refs = trigger_names_for_key(key)
        it: Dict[str, Any] = {"account": a.name, "user": a.user, "host": a.host,
                              "key": key, "used_by": refs}
        if a.password is None:
            it["action"], it["reason"] = "skipped", "no plaintext password in config"
        else:
            current = secrets_mod.get_secret(key)
            if current == a.password:
                it["action"] = "unchanged"
            elif current is None:
                it["action"] = "would import" if dry_run else "imported"
                if not dry_run:
                    _do_import(key, a.password)
            elif overwrite:
                it["action"] = "would overwrite" if dry_run else "overwritten"
                if not dry_run:
                    _do_import(key, a.password)
            else:
                it["action"] = "skipped"
                it["reason"] = "secret exists and differs (use --overwrite)"
        note = f" (himalaya '{a.name}' @ {a.host})"
        if refs:
            note += f" [used by trigger(s): {', '.join(refs)}]"
        lines.append(f"{it['action']:<14} {key}{note}"
                     + (f" — {it['reason']}" if it.get("reason") else ""))
        items.append(it)
    return {"ok": True, "config": str(path), "backend": secrets_mod.available(),
            "dry_run": dry_run, "items": items, "lines": lines}


def drift_checks(config: str = "") -> List[Dict[str, Any]]:
    """Doctor checks: himalaya password vs stored galvanize secret.

    Missing or differing secrets warn (med) — never fail the doctor, and
    never auto-resync (an operator may have rotated on the galvanize side).
    """
    path = config_path(config)
    if path is None:
        return [{"name": "himalaya", "ok": True,
                 "note": "no himalaya config found — drift check skipped"}]
    try:
        accounts = parse_accounts(path)
    except Exception as e:
        return [{"name": "himalaya", "ok": True, "warn": True,
                 "note": f"cannot parse {path}: {e}"}]
    from . import secrets as secrets_mod
    checks: List[Dict[str, Any]] = []
    for a in accounts:
        key = secret_key(a.user)
        name = f"himalaya '{a.name}'"
        if a.password is None:
            checks.append({"name": name, "ok": True,
                           "note": f"{key}: no plaintext in config — skipped"})
            continue
        current = secrets_mod.get_secret(key)
        if current == a.password:
            checks.append({"name": name, "ok": True, "note": f"{key}: in sync"})
        elif current is None:
            checks.append({"name": name, "ok": True, "warn": True,
                           "note": f"{key}: missing — run: galvanize secrets import-himalaya"})
        else:
            checks.append({"name": name, "ok": True, "warn": True,
                           "note": f"{key}: differs from himalaya — run: galvanize secrets import-himalaya --overwrite"})
    return checks
