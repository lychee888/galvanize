"""Card C6 housekeeping: relay ack-prune (F11), comment-preserving hermes
config edit (F13), legacy artifact retirement (F14).

Each test states the audit finding it pins:
  F11 — relay SQLite grew one permanent row per delivered event.
  F13 — `init` round-tripped the user's hermes config.yaml, destroying
        every comment; the sibling codex path (harnesses.register_codex)
        edited text instead. Now init does too.
  F14 — dead last_dispatch stub + source-*.json / *.bak files orphaned by
        the C5 cutover had no recycler.
"""
import hashlib
import json
import time

import pytest
import yaml

from galvanize import state as state_mod
from galvanize import store
from galvanize.relay_state import RelayState


# ------------------------------------------------------------------ F11

def _relay(tmp_path):
    return RelayState("https://relay.invalid/test")


def test_ack_prune_relines_delivered_events(tmp_path, monkeypatch):
    """Rows the ack cursor walked past are deleted on the same walk."""
    monkeypatch.chdir(tmp_path)  # RelayState writes under ensure_home()
    import galvanize.paths as paths
    monkeypatch.setenv("GALVANIZE_HOME", str(tmp_path / "gh"))
    st = RelayState("https://relay.invalid/test")
    st.stage([{"id": str(i)} for i in range(1, 6)], fetched_cursor=5)
    for i in range(1, 4):
        st.finish(str(i), "ack")
    with st.db() as db:
        rows = [r[0] for r in db.execute("SELECT id FROM events ORDER BY id")]
        assert rows == ["4", "5"], \
            "acked prefix pruned as the walk passes it; undelivered rows stay"
        assert db.execute("SELECT value FROM meta WHERE key='ack'").fetchone()[0] == "3"
    # closing the gap prunes further
    for i in (4, 5):
        st.finish(str(i), "ack")
    with st.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT value FROM meta WHERE key='ack'").fetchone()[0] == "5"
    # F11 dedupe-after-prune: a server replay of a pruned, already-acked id
    # must NOT re-dispatch — the watermark is the tombstone now.
    st.stage([{"id": "3"}], fetched_cursor=5)
    assert st.pending() == []


def test_ack_prune_keeps_failed_and_ahead_of_gap(tmp_path, monkeypatch):
    """A failed row blocks the cursor; acked rows ABOVE the gap stay until
    the walk reaches them (they are still in the ordering prefix chain)."""
    monkeypatch.setenv("GALVANIZE_HOME", str(tmp_path / "gh"))
    st = RelayState("https://relay.invalid/a")
    st.stage([{"id": "1"}, {"id": "2"}, {"id": "3"}], fetched_cursor=3)
    st.finish("1", "ack")
    for attempt in range(5):
        st.finish("2", "pending", "boom")           # exhausts -> failed
    st.finish("3", "ack")
    with st.db() as db:
        rows = {r[0]: r[1] for r in db.execute("SELECT id,state FROM events")}
        assert "1" not in rows                      # pruned (cursor at '1')
        assert rows["2"] == "failed"                # kept for retry()/failures()
        assert rows["3"] == "ack"                   # above the gap: kept
    # retry succeeds -> walk reaches '3', cursor moves, both rows gone
    assert st.retry("2")
    st.finish("2", "ack")
    with st.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert st.failures() == []


def test_quarantine_survives_pruning(tmp_path, monkeypatch):
    monkeypatch.setenv("GALVANIZE_HOME", str(tmp_path / "gh"))
    st = RelayState("https://relay.invalid/q")
    st.stage([{"nope": 1}, {"id": "2"}], fetched_cursor=2)
    st.finish("2", "ack")
    fails = st.failures()
    assert any(f["state"] == "quarantined" for f in fails)
    assert not any(f["id"] == "2" for f in fails)   # acked+pruned, not a failure


# ------------------------------------------------------------------ F13

from galvanize import cli as cli_mod


def _cfg_text(hdir, text):
    """hdir IS the hermes home (the fixture dir), not its parent."""
    hdir.mkdir(exist_ok=True)
    p = hdir / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "hermes_home", lambda: tmp_path / "hermes")
    return tmp_path / "hermes"


COMMENTED = """\
# my precious header
model: gpt-x          # inline note
providers:
  openai:
    api_key: foo      # keep me
display:
  platforms:          # NESTED platforms: NOT the top-level key
    telegram:
      streaming: true
"""


def test_f13_targeted_edit_preserves_comments_and_bytes(fake_hermes_home):
    p = _cfg_text(fake_hermes_home, COMMENTED)
    before = p.read_text(encoding="utf-8")
    assert cli_mod._enable_webhook_in_config() is True
    after = p.read_text(encoding="utf-8")
    for needle in ("# my precious header", "# inline note", "# keep me",
                   "  platforms:", "    telegram:", "      streaming: true"):
        assert needle in after, f"lost: {needle!r}"
    # append-only edit: the original bytes are an exact prefix
    assert after.startswith(before)  # order check below
    data = yaml.safe_load(after)
    assert data["platforms"]["webhook"]["enabled"] is True
    assert data["platforms"]["webhook"]["extra"]["port"] == \
        cli_mod.hermes_mod.DEFAULT_PORT
    assert data["model"] == "gpt-x"
    backup = fake_hermes_home / "config.yaml.galvanize.bak"
    assert backup.read_text(encoding="utf-8") == before


def test_f13_flips_enabled_in_place_keeps_sibling_keys(fake_hermes_home):
    text = "platforms:\n  telegram:\n    token: x   # tok\n  webhook:\n    enabled: false  # off\n"
    p = _cfg_text(fake_hermes_home, text)
    assert cli_mod._enable_webhook_in_config() is True
    after = p.read_text(encoding="utf-8")
    assert "enabled: true  # off" in after, "in-place flip keeps the line comment"
    assert "token: x   # tok" in after
    assert "telegram:" in after
    # idempotent: a second run reports "nothing changed"
    assert cli_mod._enable_webhook_in_config() is False
    assert p.read_text(encoding="utf-8") == after


def test_f13_creates_missing_keys_on_empty_file(fake_hermes_home):
    (fake_hermes_home).mkdir(parents=True, exist_ok=True)
    assert cli_mod._enable_webhook_in_config() is True
    data = yaml.safe_load((fake_hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert data["platforms"]["webhook"]["enabled"] is True
    # backup not required: there was nothing to back up
    assert not (fake_hermes_home / "config.yaml.galvanize.bak").exists()


def test_f13_refuses_clever_shape_via_roundtrip_not_corruption(fake_hermes_home):
    text = "platforms: {telegram: {token: x}}\nmodel: y\n"
    p = _cfg_text(fake_hermes_home, text)
    assert cli_mod._enable_webhook_in_config() is True
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["platforms"]["webhook"]["enabled"] is True
    assert data["platforms"]["telegram"]["token"] == "x"  # round-trip kept data


# ------------------------------------------------------------------ F14

def test_f14_last_dispatch_stub_is_gone():
    assert not hasattr(state_mod, "last_dispatch"), \
        "dead pre-C5 stub must not come back"


def _mk_live_legacy(home, bodies, ts_now):
    """Simulate a pre-C5 daemon recreating its files after our migration."""
    for key, connected in bodies.items():
        h = hashlib.sha256(key.encode()).hexdigest()
        (home / f"source-{h}.json").write_text(json.dumps({
            "connected": connected, "error": "", "ts": ts_now, "pid": 4242,
        }), encoding="utf-8")
        (home / f"source-{h}.json.bak").write_text("{}", encoding="utf-8")
    (home / "state.json").write_text(json.dumps({
        "_daemon": {"pid": 4242, "heartbeat": ts_now},
        "keep-me": {"last_fire": ts_now, "fires_today": 3,
                    "fires_date": time.strftime("%Y-%m-%d", time.gmtime(ts_now)),
                    "last_error": None, "last_error_at": None},
    }), encoding="utf-8")
    (home / "state.json.bak").write_text("{}", encoding="utf-8")
    (home / "imap_state.json").write_text(json.dumps({
        "mail-a": {"uidvalidity": "v", "last_uid": 99, "seen": [],
                   "anchored_at": ts_now}}), encoding="utf-8")
    (home / "imap_state.json.bak").write_text("{}", encoding="utf-8")
    (home / "run_hidden.vbs").write_text("' pre-C2 relic\n", encoding="utf-8")


def test_f14_prune_retires_baks_orphans_and_reimports_newer(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    home.mkdir()
    monkeypatch.setenv("GALVANIZE_HOME", str(home))
    store.reset_process_cache()
    # seed authority as C5 would: config with ONE imap trigger 'keep-me'
    store.replace_config({"keep-me": {"source": {"type": "imap", "mailbox": "a"},
                                      "wake": {"via": "hermes"}}})
    store.meta_set("migrated_at", time.time())
    store.record_fire("keep-me", True, "", now=time.time() - 10)
    t_live = time.time()
    _mk_live_legacy(home, {"imap:keep-me": True, "imap:gone-trigger": False}, t_live)
    summary = store.prune_legacy()
    files = summary["files"]
    # every legacy artifact retired
    for name in ("state.json", "imap_state.json", "state.json.bak",
                 "imap_state.json.bak", "run_hidden.vbs"):
        assert name in files, f"{name} not pruned"
        assert not (home / name).exists()
    assert not list(home.glob("source-*.json*"))
    # newer-wins re-imports landed
    st = store.trigger_state("keep-me")
    assert st["last_fire"] >= t_live and st["fires_today"] == 3
    hb = store.get_heartbeat()
    assert hb and hb["heartbeat"] >= t_live and hb["pid"] == 4242
    im = store.imap_get("mail-a")
    assert im["last_uid"] == 99
    keys = {r["key"] for r in store.read(
        lambda c: [dict(key=x[0]) for x in
                   c.execute("SELECT key FROM source_health")])}
    live_hash = hashlib.sha256(b"imap:keep-me").hexdigest()
    gone_hash = hashlib.sha256(b"imap:gone-trigger").hexdigest()
    assert f"legacy:{live_hash}" in keys, "live trigger keeps its health row"
    assert f"legacy:{gone_hash}" not in keys, "orphan legacy row pruned by name set"
    assert f"db:legacy:{gone_hash}" in files
    # idempotent second pass: nothing left to prune
    assert store.prune_legacy()["files"] == []


def test_f14_older_stale_file_does_not_clobber_db(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    home.mkdir()
    monkeypatch.setenv("GALVANIZE_HOME", str(home))
    store.reset_process_cache()
    store.replace_config({"t1": {"source": {"type": "folder", "path": str(home)},
                                 "wake": {"via": "hermes"}}})
    store.meta_set("migrated_at", time.time())
    now = time.time()
    store.record_fire("t1", True, "", now=now)          # db is FRESHER
    (home / "state.json").write_text(json.dumps({
        "t1": {"last_fire": now - 500, "fires_today": 1,
               "fires_date": "2020-01-01",
               "last_error": None, "last_error_at": None}}), encoding="utf-8")
    store.prune_legacy()
    st = store.trigger_state("t1")
    assert st["last_fire"] >= now, "stale legacy file must not roll back state"
    assert not (home / "state.json").exists()


def test_ensure_ready_runs_prune_after_migration(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    home.mkdir()
    monkeypatch.setenv("GALVANIZE_HOME", str(home))
    store.reset_process_cache()
    (home / "triggers.yaml").write_text(
        "triggers:\n  seeded:\n    source: {type: folder, path: '.'}\n"
        "    wake: {via: hermes}\n", encoding="utf-8")
    (home / "state.json.bak").write_text("{}", encoding="utf-8")
    store.ensure_ready()
    assert not (home / "state.json.bak").exists(), \
        "ensure_ready must call prune_legacy after migrating"
    store.reset_process_cache()
