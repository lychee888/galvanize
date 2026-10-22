"""Card C5 acceptance surfaces beyond the store itself: import/export
corruption drill, revision-checked editor writes (serve + dashboard shape),
serialized route writes, and the intent reconciler.
"""
import json
import threading
from pathlib import Path

import pytest

from galvanize import config as cfg
from galvanize import hermes as H
from galvanize import serve
from galvanize import store


def _emit(name):
    return cfg.Trigger(name=name, source={"type": "emit"},
                       wake={"kind": "shell", "command": "echo x"})


# ------------------------------------------------------- import/export drill

def test_import_activate_roundtrip_and_mirror(isolated_homes):
    cfg.upsert_trigger(_emit("alpha"))
    rev = cfg.config_revision()
    exported = cfg.export_yaml()
    text = exported.read_text(encoding="utf-8")
    assert "alpha:" in text
    # a hand-edit is INERT until imported (yaml is not the authority)
    edited = text.replace("alpha:", "beta:")
    (isolated_homes["galvanize"] / "side.yaml").write_text(edited, encoding="utf-8")
    assert "alpha" in cfg.load_triggers()
    r = cfg.import_yaml(Path(isolated_homes["galvanize"]) / "side.yaml")
    assert r["ok"] and r["revision"] == rev + 1
    assert "alpha" not in cfg.load_triggers() and "beta" in cfg.load_triggers()


def test_corrupt_import_keeps_last_known_good_serving(isolated_homes):
    cfg.upsert_trigger(_emit("keepme"))
    rev = cfg.config_revision()
    bad = isolated_homes["galvanize"] / "bad.yaml"
    bad.write_text("triggers:\n  broken: {source: {type: nebula}, wake: {kind: shell, command: x}}\n",
                   encoding="utf-8")
    r = cfg.import_yaml(bad)
    assert r["ok"] is False and "nebula" in r["error"]
    assert cfg.config_revision() == rev                     # nothing activated
    assert "keepme" in cfg.load_triggers()                  # LKG still serves
    st, note, _ = store.config_state()
    assert st == "corrupt"
    row = next(x for x in store.doctor_rows() if x["name"] == "config authority")
    assert row["ok"] is False and "last-known-good" in row["note"]
    # a later valid import clears the loud state
    assert cfg.import_yaml(cfg.export_yaml())["ok"]
    assert store.config_state()[0] == "ok"


def test_import_dry_run_and_revision_expectation(isolated_homes):
    cfg.upsert_trigger(_emit("a"))
    p = cfg.export_yaml()
    r = cfg.import_yaml(p, dry_run=True)
    assert r["ok"] and r["dry_run"]
    before = cfg.config_revision()
    r = cfg.import_yaml(p, expected_revision=before + 99)
    assert r.get("conflict") and cfg.config_revision() == before


# ------------------------------------------------------------- serve surface

def test_serve_set_enabled_stale_revision_409(isolated_homes, hermes_cfg):
    cfg.upsert_trigger(_emit("toggly"))
    stale = store.current_revision()
    serve.call_op("add", {"kind": "emit", "target": "", "name": "other",
                          "wake": "shell", "command": "echo 1"})   # another editor lands
    with pytest.raises(serve.ApiError) as e:
        serve.call_op("set_enabled", {"name": "toggly", "enabled": False,
                                      "expected_revision": stale})
    assert e.value.code == 409
    r = serve.call_op("set_enabled", {"name": "toggly", "enabled": False})
    assert r["ok"] and "revision" in r


def test_serve_config_ops(isolated_homes):
    cfg.upsert_trigger(_emit("viasserve"))
    r = serve.call_op("config", {"action": "revision"})
    assert r["state"] == "ok" and r["revision"] >= 1
    out = isolated_homes["galvanize"] / "x.yaml"
    r = serve.call_op("config", {"action": "export", "path": str(out)})
    assert r["ok"] and out.exists() and "viasserve" in out.read_text(encoding="utf-8")
    lst = serve.call_op("list", {})
    assert lst["revision"] == store.current_revision()
    assert "viasserve" in lst["triggers"]
    r = serve.call_op("config", {"action": "import", "path": str(out)})
    assert r["ok"]


# ----------------------------------------------------- serialized route writes

def test_concurrent_route_registration_keeps_both(isolated_homes, hermes_cfg):
    errs = []

    def reg(n):
        try:
            H.register_route(n, "s3cr3t", deliver="telegram")
        except Exception as e:          # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=reg, args=(f"race-{i}",)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    subs = json.loads((isolated_homes["hermes"] / "webhook_subscriptions.json")
                      .read_text(encoding="utf-8"))
    assert all(f"race-{i}" in subs for i in range(6))         # no lost routes
    assert len(store.route_intents()) == 6                    # intent landed per route


def test_reconciler_converges_gateway_drift(isolated_homes, hermes_cfg):
    H.register_route("drifty", "abc", deliver="telegram")
    subs_path = isolated_homes["hermes"] / "webhook_subscriptions.json"
    subs = json.loads(subs_path.read_text(encoding="utf-8"))
    # simulate the gateway (or a human) mangling OUR route
    subs["drifty"]["toolsets"] = ["web"]
    subs["ghost"] = dict(subs["drifty"], managed_by="someone-else")
    subs_path.write_text(json.dumps(subs), encoding="utf-8")

    rec = H.reconcile_routes(dry_run=True)
    assert rec["drifted"] == ["drifty"]
    rec = H.reconcile_routes()
    assert rec["repaired"] == 1
    subs = json.loads(subs_path.read_text(encoding="utf-8"))
    assert subs["drifty"]["toolsets"] == list(H.PARITY_TOOLSETS)  # intent won
    assert subs["ghost"]["toolsets"] == ["web"]                   # foreign untouched

    # a deleted managed route is resurrected from intent...
    subs = json.loads(subs_path.read_text(encoding="utf-8"))
    del subs["drifty"]
    subs_path.write_text(json.dumps(subs), encoding="utf-8")
    assert H.reconcile_routes()["repaired"] == 1
    # ...but unregister deletes intent too, so it stays gone
    assert H.unregister_route("drifty") is True
    assert H.reconcile_routes()["repaired"] == 0
    assert "drifty" not in store.route_intents()
    assert "drifty" not in json.loads(subs_path.read_text(encoding="utf-8"))


def test_migration_adopts_pre_existing_routes(isolated_homes, hermes_cfg):
    # route landed by pre-C5 code (file only, no intent row): first seed
    # adopts it as intent so the reconciler has a baseline.
    (isolated_homes["hermes"] / "webhook_subscriptions.json").write_text(
        json.dumps({"legacy": {"description": "d", "events": ["galvanize"],
                               "secret": "s", "prompt": "{prompt}",
                               "deliver": "telegram", "managed_by": "galvanize"}}),
        encoding="utf-8")
    store.ensure_ready()
    assert "legacy" in store.route_intents()


def test_route_toolsets_update_reaches_intent(isolated_homes, hermes_cfg):
    H.register_route("tooly", "s", deliver="telegram")
    assert H.set_route_toolsets("tooly", ["web", "file"])
    assert store.route_intents()["tooly"]["toolsets"] == ["web", "file"]
    assert H.set_route_toolsets("ghost-route", ["web"]) is False


def test_doctor_rows_present_and_honest(isolated_homes, hermes_cfg):
    cfg.upsert_trigger(_emit("doc"))
    d = __import__("galvanize.manage", fromlist=["manage"]).doctor()
    names = {c["name"] for c in d["checks"]}
    assert {"state store", "config authority", "routes intent"} <= names
    assert next(c for c in d["checks"] if c["name"] == "state store")["ok"]
