import json
import os
import time
from datetime import date

import pytest

from galvanize import hermes as H
from galvanize.config import Trigger, load_triggers, upsert_trigger
from galvanize.bus import TriggerBus
from galvanize.events import Event
from galvanize import manage


# ------------------------------------------------------------------ routes

def test_register_route_writes_subs_file(isolated_homes, hermes_cfg):
    url = H.register_route("cad-drops", "s3cr3t", deliver="telegram")
    assert url.endswith("/webhooks/cad-drops")
    subs = json.loads((isolated_homes["hermes"] / "webhook_subscriptions.json").read_text())
    assert subs["cad-drops"]["secret"] == "s3cr3t"
    assert subs["cad-drops"]["managed_by"] == "galvanize"
    assert subs["cad-drops"]["prompt"] == "{prompt}"


def test_register_route_splits_chat_id_from_deliver(isolated_homes, hermes_cfg):
    # The Hermes lane parses `deliver` as a bare platform name; "telegram:123"
    # passed the whole string to Platform(), every fire died at send time with
    # "Unknown deliver type" (2026-10-07: all imap triggers fired, none delivered).
    H.register_route("chat-split", "s", deliver="telegram:1683557816")
    subs = json.loads((isolated_homes["hermes"] / "webhook_subscriptions.json").read_text())
    assert subs["chat-split"]["deliver"] == "telegram"
    assert subs["chat-split"]["deliver_extra"] == {"chat_id": "1683557816"}


def test_route_empty_events_is_allow_all(isolated_homes, hermes_cfg):
    # webhook-kind routes pass events=[] to mean "accept any event type" —
    # a falsy-coercion bug used to rewrite that into the ["galvanize"]
    # filter, silently dropping every real GitHub/Stripe POST (202-shaped
    # "ignored" responses). None = default filter; [] = allow all.
    H.register_route("github-in", "s", events=[])
    subs = json.loads((isolated_homes["hermes"] / "webhook_subscriptions.json").read_text())
    assert subs["github-in"]["events"] == []
    H.register_route("internal", "s")
    subs = json.loads((isolated_homes["hermes"] / "webhook_subscriptions.json").read_text())
    assert subs["internal"]["events"] == ["galvanize"]


def test_add_webhook_trigger_route_accepts_any_event(isolated_homes, hermes_cfg):
    r = manage.add_trigger("webhook", name="gh", wake="hermes",
                           prompt="PR: {pull_request.title}")
    assert r["ok"], r
    subs = json.loads((isolated_homes["hermes"] / "webhook_subscriptions.json").read_text())
    assert subs["gh"]["events"] == []
    assert subs["gh"]["prompt"] == "PR: {pull_request.title}"


def test_register_route_refuses_clobbering_user_route(isolated_homes, hermes_cfg):
    subs_path = isolated_homes["hermes"] / "webhook_subscriptions.json"
    subs_path.write_text(json.dumps({"github": {"secret": "x", "managed_by": "human"}}))
    with pytest.raises(ValueError):
        H.register_route("github", "y")


def test_unregister_only_removes_managed(isolated_homes, hermes_cfg):
    subs_path = isolated_homes["hermes"] / "webhook_subscriptions.json"
    subs_path.write_text(json.dumps({
        "mine": {"secret": "a", "managed_by": "galvanize"},
        "yours": {"secret": "b"},
    }))
    assert H.unregister_route("mine") is True
    assert H.unregister_route("yours") is False
    assert H.unregister_route("ghost") is True


def test_secret_stable_across_re_register(isolated_homes, hermes_cfg):
    H.register_route("dup", "first")
    H.register_route("dup", "second")
    assert H.route_secret("dup") == "first"


def test_sign_v2_matches_hermes_scheme():
    # Reproduce gateway/platforms/webhook.py _validate_signature V2 branch
    import hashlib, hmac
    secret, body, ts = "topsecret", b'{"a":1}', int(time.time())
    expected = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    assert H.sign_v2(secret, body, ts) == expected


# ------------------------------------------------------------------ add/remove

def test_add_folder_and_remove(tmp_path, isolated_homes):
    watch = tmp_path / "drops"
    watch.mkdir()
    # no hermes webhook configured: wake=shell keeps this test self-contained
    r = manage.add_trigger("folder", str(watch), name="drops", wake="shell",
                           command="echo woken", patterns=["*.step"])
    assert r["ok"], r
    ts = load_triggers()
    assert "drops" in ts
    assert ts["drops"].source["patterns"] == ["*.step"]
    assert ts["drops"].dedupe_key == "{path}"
    r2 = manage.remove_trigger_op("drops")
    assert r2["ok"] and "drops" not in load_triggers()


def test_add_folder_autocreates(isolated_homes, tmp_path):
    # UX: watching a folder that doesn't exist yet is the normal case.
    target = tmp_path / "watch-me" / "deeper"
    r = manage.add_trigger("folder", str(target), wake="shell", command="echo")
    assert r["ok"], r
    assert target.is_dir()
    # an empty target is still an error (fails name validation first)
    r2 = manage.add_trigger("folder", "", wake="shell", command="echo")
    assert not r2["ok"]


def test_add_hermes_registers_route(isolated_homes, hermes_cfg):
    r = manage.add_trigger("emit", name="ping", wake="hermes", deliver="telegram")
    assert r["ok"], r
    assert H.route_secret("ping"), "route + secret registered"


def test_name_validation(isolated_homes):
    r = manage.add_trigger("emit", name="Bad Name!", wake="shell", command="echo")
    assert not r["ok"]


# ------------------------------------------------------------------ bus guards

def _trigger(**kw):
    base = dict(name="t", source={"type": "emit"}, wake={"kind": "shell", "command": "echo ok"})
    base.update(kw)
    return Trigger(**base)


def test_cooldown_suppresses_second_fire(tmp_path, isolated_homes):
    t = _trigger(cooldown_s=60)
    bus = TriggerBus()
    ok1, d1 = bus.handle(t, Event("t", "test", "x", {"n": 1}))
    ok2, d2 = bus.handle(t, Event("t", "test", "x", {"n": 2}))
    assert ok1 and "skipped" not in d1
    assert ok2 and "cooldown" in d2


def test_dedupe_collapses_same_key(tmp_path, isolated_homes):
    t = _trigger(dedupe_key="{path}")
    bus = TriggerBus()
    ok1, d1 = bus.handle(t, Event("t", "folder", "file.created", {"path": "/x/a.step"}))
    ok2, d2 = bus.handle(t, Event("t", "folder", "file.created", {"path": "/x/a.step"}))
    ok3, d3 = bus.handle(t, Event("t", "folder", "file.created", {"path": "/x/b.step"}))
    assert "skipped" not in d1
    assert "dedupe" in d2
    assert "skipped" not in d3


def test_dispatch_failure_not_recorded_as_dedupe(tmp_path, isolated_homes):
    t = _trigger(wake={"kind": "shell", "command": "exit 3"}, dedupe_key="{path}")
    bus = TriggerBus()
    ok1, d1 = bus.handle(t, Event("t", "folder", "file.created", {"path": "/x/a"}))
    assert not ok1 and "exit 3" in d1
    ok2, d2 = bus.handle(t, Event("t", "folder", "file.created", {"path": "/x/a"}))
    assert not ok2 and "dedupe" not in d2  # failure must not poison the dedupe key


def test_wake_preset_codex_adds_successfully(isolated_homes):
    """--wake codex compiles to a shell wake with the preset command
    (regression: wake was renamed before the kind check, breaking presets)."""
    r = manage.add_trigger("emit", name="preset-cx", wake="codex")
    assert r["ok"], r
    from galvanize.config import load_triggers
    t = load_triggers()["preset-cx"]
    assert t.wake["kind"] == "shell" and "codex exec" in t.wake["command"]


# ---------------------------------------------------- daemon liveness / status

def test_live_daemon_pid_respects_heartbeat_age(isolated_homes):
    from galvanize import state as st
    assert st.live_daemon_pid() is None            # no heartbeat at all
    st.set_heartbeat()
    assert st.live_daemon_pid() == os.getpid()     # fresh beat -> this pid
    p = isolated_homes["galvanize"] / "state.json"
    data = json.loads(p.read_text())
    data["_daemon"]["heartbeat"] -= 120            # force stale
    p.write_text(json.dumps(data))
    assert st.live_daemon_pid() is None


def test_daemon_run_refuses_second_instance(isolated_homes, monkeypatch, capsys):
    from galvanize import state as st
    from galvanize.daemon import Daemon
    st.set_heartbeat()                             # marks THIS pid alive…
    st._save({**st._load(), "_daemon": {"heartbeat": __import__("time").time(), "pid": 999999}})
    Daemon().run()                                 # …so run() must refuse, not double-watch
    assert "already running" in capsys.readouterr().out


def test_status_fires_today_requires_matching_date(isolated_homes, hermes_cfg):
    from galvanize import manage
    from galvanize.state import state_path
    yesterday = (date.today() - __import__("datetime").timedelta(days=1)).isoformat()
    state_path().write_text(json.dumps({
        "t1": {"fires_date": yesterday, "fires_today": 4, "last_fire": None}}))
    upsert_trigger(Trigger(name="t1", source={"type": "emit"}, wake={"kind": "log"}))
    row = next(r for r in manage.status()["triggers"] if r["name"] == "t1")
    assert row["fires_today"] == 0                 # stale count must not render as today's
