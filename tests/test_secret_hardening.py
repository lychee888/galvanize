"""Audit 2026-10-20 hardening: F1 fail-loud secrets, F2 racy keyring probe,
F3 stale-health masking. Maps to deliverables/audit-2026-10-20.md.

Zero hardware: fake in-memory keyring module + fake MailBox; no network,
no real credentials, no Docker.

Test -> finding map:
  F1: test_f1_missing_secret_fails_loud_without_login,
      test_f1_surfaces_missing_keyring_backend,
      test_f1_explicit_password_still_logs_in
  F2: test_f2_probe_runs_once_per_process,
      test_f2_probe_key_is_pid_scoped,
      test_f2_delete_failure_is_non_fatal,
      test_f2_concurrent_gets_never_miss
  F3: test_f3_error_survives_window_when_writer_is_live,
      test_f3_dead_writer_still_reports_stale
"""

import json
import os
import sys
import threading
import time
import types

import pytest

from galvanize import secrets, state
from galvanize.config import Trigger
from galvanize.sources import imap as imap_mod
from galvanize.sources.imap import ImapWatcher


# --------------------------------------------------------------------------
# fake keyring harness
# --------------------------------------------------------------------------

class PasswordNotFound(Exception):
    pass


def install_fake_keyring(monkeypatch, *, delete_always_fails=False):
    """sys.modules-inject an in-memory keyring with real-ish semantics.

    Returns the call record: {"sets": [keys], "deletes": n, "data": {...}}.
    """
    rec = {"sets": [], "deletes": 0, "data": {}}
    lock = threading.Lock()

    class Backend:
        name = "FakeVault"

    def set_password(svc, key, value):
        with lock:
            rec["data"][(svc, key)] = value
            rec["sets"].append(key)

    def get_password(svc, key):
        with lock:
            return rec["data"].get((svc, key))

    def delete_password(svc, key):
        with lock:
            rec["deletes"] += 1
            if delete_always_fails:
                raise PasswordNotFound(key)
            if rec["data"].pop((svc, key), None) is None:
                raise PasswordNotFound(key)

    mod = types.ModuleType("keyring")
    mod.set_password = set_password
    mod.get_password = get_password
    mod.delete_password = delete_password
    mod.get_keyring = lambda: Backend()
    errors = types.ModuleType("keyring.errors")
    errors.PasswordNotFound = PasswordNotFound
    errors.NoKeyringError = type("NoKeyringError", (Exception,), {})
    mod.errors = errors
    monkeypatch.setitem(sys.modules, "keyring", mod)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)
    # Fresh probe state per test (attribute is the fix's memo; raising=False
    # keeps this file runnable against the pre-fix code as red tests).
    monkeypatch.setattr(secrets, "_probe_ok", None, raising=False)
    return rec


# --------------------------------------------------------------------------
# fake MailBox harness (F1)
# --------------------------------------------------------------------------

class FakeBox:
    """Records login attempts; raises after login so _open stops early."""

    instances = []

    def __init__(self, host, port=None, timeout=None, *a, **k):
        self.host, self.port, self.logged = host, port, None
        FakeBox.instances.append(self)

    def login(self, user, pw):
        self.logged = (user, pw)
        raise RuntimeError("stop-after-login")

    xoauth2 = login


@pytest.fixture()
def boom_boxes(monkeypatch):
    """Any watcher login attempt trips: no login without a real secret."""
    FakeBox.instances = []
    monkeypatch.setattr(imap_mod, "MailBox", FakeBox)
    monkeypatch.setattr(imap_mod, "MailBoxUnencrypted", FakeBox)
    return FakeBox


def _imap_trigger(name="acct", **src_over):
    src = {"type": "imap", "host": "h.example", "user": "u@x.example",
           "secret_key": f"imap:{name}"}
    src.update(src_over)
    return Trigger(name, src, {"kind": "shell"})


# --------------------------------------------------------------------------
# F1 — silent empty-password auth
# --------------------------------------------------------------------------

def test_f1_missing_secret_fails_loud_without_login(boom_boxes):
    w = ImapWatcher(_imap_trigger(), lambda e: None,
                    password_provider=lambda: None, secret_key="imap:acct")
    with pytest.raises(imap_mod.SecretUnavailable):
        w._session()
    assert boom_boxes.instances == [], "login was attempted with a falsy secret"
    assert w.connected is False
    h = state.source_health("imap:acct")
    assert h["connected"] is False
    assert "secret 'imap:acct' unavailable" in h["error"]


def test_f1_surfaces_missing_keyring_backend(boom_boxes, monkeypatch):
    monkeypatch.setattr(secrets, "available", lambda: "file")
    w = ImapWatcher(_imap_trigger(), lambda e: None,
                    password_provider=lambda: None, secret_key="imap:acct")
    with pytest.raises(imap_mod.SecretUnavailable):
        w._session()
    h = state.source_health("imap:acct")
    assert "no OS keyring" in h["error"], h["error"]


def test_f1_explicit_password_still_logs_in(boom_boxes):
    t = _imap_trigger(password="app-pw")
    w = ImapWatcher(t, lambda e: None, password_provider=lambda: None)
    with pytest.raises(RuntimeError):  # FakeBox raises after login
        w._open()
    assert boom_boxes.instances[0].logged == ("u@x.example", "app-pw")


# --------------------------------------------------------------------------
# F2 — racy, self-sabotaging keyring probe
# --------------------------------------------------------------------------

def test_f2_probe_runs_once_per_process(monkeypatch):
    rec = install_fake_keyring(monkeypatch)
    for _ in range(50):
        assert secrets._keyring_backend_ok() is True
    probe_sets = [k for k in rec["sets"] if k.startswith("__probe__")]
    assert len(probe_sets) == 1, f"probe ran {len(probe_sets)}x; must be once/process"


def test_f2_probe_key_is_pid_scoped(monkeypatch):
    rec = install_fake_keyring(monkeypatch)
    assert secrets._keyring_backend_ok() is True
    assert rec["sets"] == [f"__probe__{os.getpid()}"], \
        "probe must use a per-process key, never a shared '__probe__'"


def test_f2_delete_failure_is_non_fatal(monkeypatch):
    install_fake_keyring(monkeypatch, delete_always_fails=True)
    assert secrets._keyring_backend_ok() is True, \
        "a losing probe delete must not mark the backend unusable"


def test_f2_concurrent_gets_never_miss(monkeypatch):
    install_fake_keyring(monkeypatch)
    secrets.set_secret("imap:acct", "hunter2")
    # Force every thread to face a cold probe: pre-fix, concurrent probes
    # sabotage each other's delete and 16x10 gets return None intermittently.
    reset = getattr(secrets, "_reset_probe_cache", None)
    if reset:
        reset()
    misses = []

    def worker():
        for _ in range(10):
            if secrets.get_secret("imap:acct") != "hunter2":
                misses.append(1)

    ts = [threading.Thread(target=worker) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not misses, f"{len(misses)}/160 threaded get_secret misses"


# --------------------------------------------------------------------------
# F3 — "stale watcher health" masks the actionable error
# --------------------------------------------------------------------------

def _age_health_file(key, seconds):
    p = state._health_path(key)
    h = json.loads(p.read_text(encoding="utf-8"))
    h["ts"] = time.time() - seconds
    p.write_text(json.dumps(h), encoding="utf-8")


def test_f3_error_survives_window_when_writer_is_live():
    state.set_source_health("imap:acct", False,
                            "MailboxLoginError: empty password")
    _age_health_file("imap:acct", 200)  # mid-backoff: written >60s ago
    got = state.source_health("imap:acct", daemon_pid=os.getpid())
    assert got["connected"] is False
    assert "MailboxLoginError" in got["error"], \
        "stored error must survive the 60s window when the writer is the live daemon"
    assert "stale watcher health" not in got["error"]
    assert "s ago" in got["error"], "age annotation required"


def test_f3_dead_writer_still_reports_stale():
    state.set_source_health("imap:acct", False, "MailboxLoginError: gone")
    _age_health_file("imap:acct", 5)
    got = state.source_health("imap:acct", daemon_pid=os.getpid() + 9999)
    assert got["connected"] is False
    assert got["error"] == "stale watcher health", \
        "pid mismatch (dead daemon) must still read stale"
