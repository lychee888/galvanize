"""Card C5: the SQLite authority — migration, revisions, race safety.

Acceptance pieces owned here: stale-revision rejection, two-process
compound-update race, idempotent migration, missing-vs-corrupt config
states, loud doctor rows.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from galvanize import store  # noqa: E402


# ------------------------------------------------------------------ fixtures

GOOD_YAML = """
triggers:
  drops:
    source: {type: emit}
    wake: {kind: shell, command: 'echo hi'}
    prompt: hello
  mail:
    source: {type: imap, host: imap.example.com, user: a@b.c, secret_key: 'imap:a@b.c'}
    wake: {kind: hermes, deliver: telegram}
"""


def _write_legacy(home: Path) -> None:
    (home / "triggers.yaml").write_text(GOOD_YAML, encoding="utf-8")
    (home / "state.json").write_text(json.dumps({
        "drops": {"last_fire": 100.0, "last_error": None,
                  "fires_date": "2026-01-01", "fires_today": 3},
        "mail": {"last_error": "boom", "last_error_at": 90.0},
        "_daemon": {"heartbeat": 111.0, "pid": 4242},
    }), encoding="utf-8")
    (home / "imap_state.json").write_text(json.dumps({
        "mail": {"uidvalidity": "7", "last_uid": 500,
                 "seen": [499, 500], "anchored_at": 80.0},
    }), encoding="utf-8")
    (home / "source-abc123.json").write_text(json.dumps(
        {"connected": True, "error": "", "ts": 105.0, "pid": 4242}), encoding="utf-8")


# ------------------------------------------------------------------ bootstrap

def test_fresh_store_seeds_from_yaml(isolated_homes):
    home = isolated_homes["galvanize"]
    _write_legacy(home)
    summary = store.ensure_ready()
    assert summary["skipped"] is False
    assert summary["state_entries"] == 3          # 2 triggers + _daemon
    assert summary["imap_entries"] == 1
    assert summary["health_entries"] == 1
    bodies = store.config_bodies()
    assert set(bodies) == {"drops", "mail"}
    assert bodies["drops"]["source"]["type"] == "emit"
    assert store.current_revision() == 1
    st, note, rev = store.config_state()
    assert (st, rev) == ("ok", 1)


def test_migration_is_idempotent_and_retires_files(isolated_homes):
    home = isolated_homes["galvanize"]
    _write_legacy(home)
    store.ensure_ready()
    assert not (home / "state.json").exists()
    # C5 kept the renamed originals "until next release"; card C6 IS that
    # release: prune_legacy runs right after ensure_ready and cashes the
    # promise in. migrate_from_files itself still only renames (rollback
    # belt within a single release train).
    assert (home / "state.json.bak").exists() is False
    assert (home / "imap_state.json.bak").exists() is False
    assert (home / "source-abc123.json.bak").exists() is False
    # a second migration does nothing
    second = store.migrate_from_files(home)
    assert second["skipped"] is True
    assert store.current_revision() == 1
    assert store.trigger_state("drops")["fires_today"] == 3
    assert store.get_heartbeat() == {"pid": 4242, "heartbeat": 111.0}


def test_missing_vs_corrupt_yaml_distinguished(isolated_homes):
    home = isolated_homes["galvanize"]
    # missing entirely -> 'never' + UNHEALTHY doctor row (not "no triggers")
    store.reset_process_cache()
    store.ensure_ready()
    st, note, rev = store.config_state()
    assert st == "never" and rev == 0
    rows = [r for r in store.doctor_rows() if r["name"] == "config authority"]
    assert rows and rows[0]["ok"] is False and "UNHEALTHY" in rows[0]["note"]

    # corrupt content -> 'corrupt', nothing activated, loud doctor
    (home / "triggers.yaml").write_text("triggers: {drops: [unclosed\n", encoding="utf-8")
    conn = store.connect()
    conn.execute("DELETE FROM meta WHERE key IN ('migrated_at','config_state','config_note')")
    conn.execute("INSERT INTO meta(key,value) VALUES('cfg_rev','0')")
    conn.execute("DELETE FROM trigger_config")
    conn.commit()
    conn.close()
    store.reset_process_cache()
    store.ensure_ready()
    st, note, rev = store.config_state()
    assert st == "corrupt"
    assert rev == 0 and store.config_bodies() == {}
    rows = [r for r in store.doctor_rows() if r["name"] == "config authority"]
    assert rows[0]["ok"] is False and "last-known-good" in rows[0]["note"]


def test_structurally_invalid_seed_rejected_wholesale(isolated_homes):
    home = isolated_homes["galvanize"]
    (home / "triggers.yaml").write_text(
        "triggers:\n"
        "  ok1: {source: {type: emit}, wake: {kind: shell, command: x}}\n"
        "  bad1: {source: {type: nebula}, wake: {kind: shell, command: x}}\n",
        encoding="utf-8")
    store.ensure_ready()
    st, note, rev = store.config_state()
    assert st == "corrupt" and "nebula" in note
    assert store.config_bodies() == {}   # all-or-nothing, not partial activation


# ------------------------------------------------------------- revisions (CAS)

def test_stale_revision_rejected_changes_nothing(isolated_homes):
    home = isolated_homes["galvanize"]
    (home / "triggers.yaml").write_text(GOOD_YAML, encoding="utf-8")
    store.ensure_ready()
    stale = store.current_revision()
    # another editor lands first
    store.upsert_body("third", {"source": {"type": "emit"}, "wake": {"kind": "shell",
                                                                     "command": "y"}})
    with pytest.raises(store.StaleRevisionError):
        store.upsert_body("squatter", {"source": {"type": "emit"},
                                       "wake": {"kind": "log"}},
                          expected_revision=stale)
    bodies = store.config_bodies()
    assert "squatter" not in bodies                 # rejected write left no trace
    assert "third" in bodies                        # fresh edit survived
    assert store.current_revision() == stale + 1


def test_revision_bumps_once_per_compound_mutation(isolated_homes):
    store.ensure_ready()
    r0 = store.current_revision()
    def fn(bodies):
        bodies["a"] = {"source": {"type": "emit"}, "wake": {"kind": "shell", "command": "x"}}
        bodies["b"] = {"source": {"type": "emit"}, "wake": {"kind": "shell", "command": "y"}}
    r1 = store.mutate_config(fn)
    assert r1 == r0 + 1   # one transaction, one revision, two triggers


# ---------------------------------------------------------------- race tests

def test_concurrent_record_fire_loses_no_count(isolated_homes):
    store.ensure_ready()
    errors = []

    def hammer(i):
        try:
            for _ in range(40):
                store.record_fire("race", ok=(i % 2 == 0), detail="x" if i % 2 else "")
        except Exception as e:  # pragma: no cover - failure surface
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    st = store.trigger_state("race")
    assert st["fires_today"] == 120            # 3 threads x 40 ok-fires, exact
    hist = store.fire_history(limit=300)
    assert len(hist) == 240                        # every fire is its own row
    assert sum(1 for h in hist if h["ok"]) == 120  # ok-fires counted exactly


_WORKER = """
import sys
sys.path.insert(0, {repo!r})
from galvanize import store
for i in range(25):
    store.mutate_config(lambda b, i=i: b.update({{f"w{w}-{{i}}":
        {{"source": {{"type": "emit"}}, "wake": {{"kind": "shell", "command": "x"}}}}}}))
"""


def test_two_processes_compound_update_serialize(isolated_homes):
    store.ensure_ready()
    base = store.current_revision()
    env = dict(os.environ)
    procs = [subprocess.Popen(
        [sys.executable, "-c", _WORKER.format(repo=str(REPO), w=w)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for w in ("A", "B")]
    codes = [p.wait(timeout=120) for p in procs]
    assert codes == [0, 0]
    assert store.current_revision() == base + 50   # no lost writes
    bodies = store.config_bodies()
    assert sum(1 for k in bodies if k.startswith(("wA-", "wB-"))) == 50


def test_heartbeat_and_fire_are_independent_rows(isolated_homes):
    store.ensure_ready()
    store.set_heartbeat(pid=777)
    store.record_fire("hb", ok=True)
    assert store.get_heartbeat()["pid"] == 777   # a fire never clobbers the beat
    store.set_heartbeat(pid=777)
    store.clear_heartbeat()
    assert store.get_heartbeat() is None         # nor does clearing touch state
    assert store.trigger_state("hb").get("fires_today") == 1


def test_wal_mode_active(isolated_homes):
    store.ensure_ready()
    def _q(conn):
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert store.read(_q) == "wal"


def test_imap_state_single_row_per_watcher(isolated_homes):
    store.ensure_ready()
    store.imap_put("m1", {"uidvalidity": "1", "last_uid": 10, "seen": [9, 10]})
    store.imap_put("m2", {"uidvalidity": "2", "last_uid": 20, "seen": [18, 19, 20]})
    assert store.imap_get("m1")["last_uid"] == 10
    assert store.imap_get("m2")["seen"] == [18, 19, 20]
    assert store.imap_get("ghost") == {}


def test_corrupt_db_reports_loud_not_silent(isolated_homes):
    store.ensure_ready()
    store.record_fire("keep", ok=True)
    path = store.db_path()
    rows_before = store.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM trigger_state").fetchone()[0])
    assert rows_before >= 1
    # a WAL-mode db that is not a database at all must raise, not read empty
    path.write_bytes(b"this is not sqlite" + os.urandom(64))
    store.reset_process_cache()
    with pytest.raises(Exception):
        store.ensure_ready()


def test_concurrent_first_opens_survive_the_wal_flip(tmp_path, monkeypatch):
    """Release-gate flake (card C6): the journal_mode=WAL flip does not
    invoke sqlite's busy handler, so on a FRESH db a concurrent writer
    made open fail with an immediate 'database is locked'. connect() now
    retries the flip inside the busy budget; this test pounds it."""
    import threading
    for rnd in range(8):
        home = tmp_path / f"h{rnd}"
        home.mkdir()
        monkeypatch.setenv("GALVANIZE_HOME", str(home))
        store.reset_process_cache()
        errs = []

        def w():
            try:
                with store.write_tx() as c:
                    c.execute("INSERT OR REPLACE INTO meta VALUES('k','v')")
            except Exception as e:          # noqa: BLE001 - the point
                errs.append(e)

        ts = [threading.Thread(target=w) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errs, f"round {rnd}: {errs[:2]}"
