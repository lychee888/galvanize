"""Secret storage: OS keyring with an encrypted-at-rest boundary fallback.

Service namespace: "galvanize". Keys like "imap:ktt" -> password.
Backend: `keyring` package (Windows Credential Manager / macOS Keychain /
Secret Service). When no usable backend exists (headless CI, minimal
containers), fall back to a 0600 JSON file in GALVANIZE_HOME and loudly
prefer migration the moment a keyring appears.

Nothing else in the codebase reads/writes credentials directly.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .paths import ensure_home, galvanize_home

SERVICE = "galvanize"
_FALLBACK_NAME = "secrets.json"


def _fallback_path() -> Path:
    return galvanize_home() / _FALLBACK_NAME


# F2 (audit 2026-10-20): the probe used to run on EVERY call with one shared
# "__probe__" credential; concurrent watchers' set/get/delete interleavings
# made a loser's delete raise PasswordNotFound -> probe False -> file fallback
# -> get_secret None -> silent empty-password logins (20-55/80 misses measured
# with 4 threads against Windows Credential Manager). Fix: probe at most once
# per process (memoized under a lock), key it per-pid so sibling processes
# can't collide, and treat a failed delete as harmless cleanup noise.
_PROBE_KEY = f"__probe__{os.getpid()}"
_probe_ok: Optional[bool] = None
_probe_lock = threading.Lock()


def _reset_probe_cache() -> None:
    """Drop the memo so the next call re-probes (tests; daemons pick up a
    newly-appeared keyring via restart)."""
    global _probe_ok
    with _probe_lock:
        _probe_ok = None


def _keyring_backend_ok() -> bool:
    global _probe_ok
    if _probe_ok is not None:
        return _probe_ok
    with _probe_lock:
        if _probe_ok is None:
            _probe_ok = _probe_keyring_once()
        return _probe_ok


def _probe_keyring_once() -> bool:
    try:
        import keyring
        from keyring.errors import NoKeyringError
        try:
            # Probe without triggering GUI unlock prompts on macOS:
            backend = keyring.get_keyring()
            name = getattr(backend, "name", "") or type(backend).__name__
            if "fail" in name.lower() or "null" in name.lower():
                return False
            # A trivial write/read proves usability on Windows Credential Manager
            keyring.set_password(SERVICE, _PROBE_KEY, "1")
            ok = keyring.get_password(SERVICE, _PROBE_KEY) == "1"
            try:
                keyring.delete_password(SERVICE, _PROBE_KEY)
            except Exception:
                pass  # delete is cleanup; its failure never invalidates the ok read
            return ok
        except (NoKeyringError, Exception):
            return False
    except ImportError:
        return False


def _fallback_store() -> dict:
    p = _fallback_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fallback_save(store: dict) -> None:
    ensure_home()
    p = _fallback_path()
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def available() -> str:
    """'keyring' | 'file' — where secrets will land."""
    return "keyring" if _keyring_backend_ok() else "file"


def set_secret(key: str, value: str) -> str:
    """Store a secret. Returns the backend used ('keyring'|'file')."""
    if _keyring_backend_ok():
        import keyring
        keyring.set_password(SERVICE, key, value)
        # migrate any stale fallback copy away
        store = _fallback_store()
        if store.pop(key, None) is not None:
            _fallback_save(store)
        return "keyring"
    store = _fallback_store()
    store[key] = value
    _fallback_save(store)
    return "file"


def get_secret(key: str) -> Optional[str]:
    if _keyring_backend_ok():
        try:
            import keyring
            v = keyring.get_password(SERVICE, key)
            if v is not None:
                return v
        except Exception:
            pass
    return _fallback_store().get(key)


def delete_secret(key: str) -> bool:
    existed = False
    if _keyring_backend_ok():
        try:
            import keyring
            if keyring.get_password(SERVICE, key) is not None:
                keyring.delete_password(SERVICE, key)
                existed = True
        except Exception:
            pass
    store = _fallback_store()
    if store.pop(key, None) is not None:
        _fallback_save(store)
        existed = True
    return existed


def list_keys() -> list:
    keys = set(_fallback_store().keys())
    if _keyring_backend_ok():
        try:
            # keyring has no portable enumeration; track our own index.
            idx = _fallback_store().get("__keyring_index__")
            if isinstance(idx, list):
                keys.update(str(k) for k in idx)
        except Exception:
            pass
    return sorted(k for k in keys if not k.startswith("__"))


def index_keyring_key(key: str) -> None:
    """Record a keyring-stored key name (not its value) for enumeration."""
    store = _fallback_store()
    idx = store.setdefault("__keyring_index__", [])
    if key not in idx:
        idx.append(key)
        _fallback_save(store)


def migrate_note() -> str:
    """One-line status for `doctor`."""
    backend = available()
    leftover = [k for k in _fallback_store() if not k.startswith("__")]
    if backend == "keyring":
        if leftover:
            return f"keyring active; {len(leftover)} secret(s) still in file: {', '.join(leftover)}"
        return "keyring active (OS credential store)"
    return "WARNING: no OS keyring — secrets in plaintext file (0600): " + str(_fallback_path())
