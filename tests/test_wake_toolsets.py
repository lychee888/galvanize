"""Wake sessions & tool access: per-route toolsets (v0.2.4, parity-by-default).

Hermes webhook routes used to run on the gateway's restricted `hermes-webhook`
toolset (4 tools, no terminal/file), which silently crippled every wake
prompt containing real steps. v0.2.4 writes the gateway's `default`
distribution names ("all available tools, all the time") as the route's
"toolsets" override BY DEFAULT; `--wake-tools` picks an exact subset and
`--wake-tools none` opts back into the restricted alert-only posture.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from galvanize import hermes as H
from galvanize import manage

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "hermes" / "__init__.py"

# Exact v0.2.3 route dict — the restricted / absent-key shape.
V023_ROUTE = {
    "description": "galvanize trigger: plain",
    "events": ["galvanize"],
    "secret": "s",
    "prompt": "{prompt}",
    "deliver": "log",
    "managed_by": "galvanize",
}


def _subs(isolated_homes):
    return json.loads(
        (isolated_homes["hermes"] / "webhook_subscriptions.json").read_text())


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("gz_hermes_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- register_route

def test_default_registration_writes_parity_list(isolated_homes, hermes_cfg):
    H.register_route("plain", "s")
    route = _subs(isolated_homes)["plain"]
    assert route["toolsets"] == list(H.PARITY_TOOLSETS)
    # The parity list IS the gateway's `default` distribution at 100%:
    # web, vision, image_gen, terminal, file, browser (sorted here).
    assert set(route["toolsets"]) == {
        "web", "vision", "image_gen", "terminal", "file", "browser"}
    # Every other field is untouched v0.2.3 content.
    assert {k: v for k, v in route.items() if k != "toolsets"} == V023_ROUTE


def test_parity_list_matches_installed_gateway():
    """Drift guard: PARITY_TOOLSETS must equal the `default` distribution's
    members whenever a real hermes-agent checkout sits next to the default
    (unisolated) Hermes home."""
    import os
    import sys
    if sys.platform == "win32":
        la = os.environ.get("LOCALAPPDATA", "").strip()
        home = (Path(la) if la else Path.home() / "AppData" / "Local") / "hermes"
    else:
        home = Path.home() / ".hermes"
    src = home / "hermes-agent"
    if not (src / "toolset_distributions.py").exists():
        pytest.skip("no local hermes-agent checkout to cross-check")
    sys.path.insert(0, str(src))
    try:
        import importlib
        td = importlib.reload(importlib.import_module("toolset_distributions")) \
            if "toolset_distributions" in sys.modules else importlib.import_module("toolset_distributions")
        members = sorted(n for e in td.DISTRIBUTIONS["default"]["toolsets"]
                         for n in e.split("+"))
        assert members == sorted(H.PARITY_TOOLSETS)
    finally:
        sys.path.remove(str(src))


def test_subset_written_verbatim(isolated_homes, hermes_cfg):
    H.register_route("sub", "s", toolsets=["terminal", "file"])
    assert _subs(isolated_homes)["sub"]["toolsets"] == ["terminal", "file"]


def test_empty_list_is_explicit_restricted_no_key(isolated_homes, hermes_cfg):
    # `--wake-tools none` -> [] -> key absent -> gateway's restricted
    # hermes-webhook default. Route bytes match v0.2.3 exactly.
    H.register_route("restr", "s", toolsets=[])
    route = _subs(isolated_homes)["restr"]
    assert route == V023_ROUTE | {"description": "galvanize trigger: restr"}
    assert list(route.keys()) == list(V023_ROUTE.keys())


def test_toolsets_strips_blanks(isolated_homes, hermes_cfg):
    H.register_route("scrub", "s", toolsets=[" terminal ", "", "file"])
    assert _subs(isolated_homes)["scrub"]["toolsets"] == ["terminal", "file"]


def test_readd_keeps_custom_grant_upgrades_legacy(isolated_homes, hermes_cfg):
    # No-arg re-add must not revoke an explicit subset...
    H.register_route("dup", "s", toolsets=["terminal"])
    H.register_route("dup", "s")
    assert _subs(isolated_homes)["dup"]["toolsets"] == ["terminal"]
    # ...nor keep a legacy key-less route crippled: re-registration is a fix.
    subs_path = isolated_homes["hermes"] / "webhook_subscriptions.json"
    data = _subs(isolated_homes)
    del data["dup"]["toolsets"]           # simulate pre-v0.2.4 route
    subs_path.write_text(json.dumps(data))
    H.register_route("dup", "s")
    assert _subs(isolated_homes)["dup"]["toolsets"] == list(H.PARITY_TOOLSETS)
    # An explicit list replaces the old grant.
    H.register_route("dup", "s", toolsets=["web", "browser"])
    assert _subs(isolated_homes)["dup"]["toolsets"] == ["web", "browser"]
    # And an explicit [] demotes back to restricted.
    H.register_route("dup", "s", toolsets=[])
    assert "toolsets" not in _subs(isolated_homes)["dup"]


def test_route_toolsets_accessor(isolated_homes, hermes_cfg):
    H.register_route("gr", "s", toolsets=["terminal"])
    assert H.route_toolsets("gr") == ["terminal"]
    H.register_route("restr", "s", toolsets=[])
    assert H.route_toolsets("restr") is None
    assert H.route_toolsets("ghost") is None
    # Foreign (user-created) routes are never reported as ours.
    subs_path = isolated_homes["hermes"] / "webhook_subscriptions.json"
    data = _subs(isolated_homes)
    data["human"] = {"secret": "x", "toolsets": ["terminal"]}
    subs_path.write_text(json.dumps(data))
    assert H.route_toolsets("human") is None


# ---------------------------------------------------------------- add_trigger op

def test_add_trigger_default_hermes_gets_parity(isolated_homes, hermes_cfg):
    r = manage.add_trigger("emit", name="ping", wake="hermes")
    assert r["ok"], r
    assert _subs(isolated_homes)["ping"]["toolsets"] == list(H.PARITY_TOOLSETS)
    # No explicit choice -> nothing recorded in the trigger's wake cfg.
    from galvanize.config import load_triggers
    assert "toolsets" not in load_triggers()["ping"].wake


def test_add_trigger_forwards_wake_tools_subset(isolated_homes, hermes_cfg):
    r = manage.add_trigger("emit", name="ping", wake="hermes",
                           wake_tools=["terminal", "file"])
    assert r["ok"], r
    assert _subs(isolated_homes)["ping"]["toolsets"] == ["terminal", "file"]
    from galvanize.config import load_triggers
    assert load_triggers()["ping"].wake["toolsets"] == ["terminal", "file"]


def test_add_trigger_none_records_and_survives_readd(isolated_homes, hermes_cfg):
    # Explicit restricted choice is recorded in wake cfg so a no-flag
    # re-add does NOT silently upgrade it to parity.
    assert manage.add_trigger("emit", name="noisy", wake="hermes",
                              wake_tools=[])["ok"]
    assert "toolsets" not in _subs(isolated_homes)["noisy"]
    assert manage.add_trigger("emit", name="noisy", wake="hermes")["ok"]
    assert "toolsets" not in _subs(isolated_homes)["noisy"]


def test_add_trigger_wake_tools_requires_hermes_wake(isolated_homes, tmp_path):
    r = manage.add_trigger("folder", str(tmp_path / "d"), name="shl",
                           wake="shell", command="echo hi",
                           wake_tools=["terminal"])
    assert not r["ok"]
    assert "hermes" in r["error"].lower()
    # Explicit restriction is equally meaningless for non-hermes wakes.
    r0 = manage.add_trigger("emit", name="shl2", wake="shell", command="echo",
                            wake_tools=[])
    assert not r0["ok"] and "hermes" in r0["error"].lower()
    # Harness presets are shell wakes under the hood.
    r2 = manage.add_trigger("emit", name="clau", wake="claude",
                            wake_tools=["terminal"])
    assert not r2["ok"] and "hermes" in r2["error"].lower()


# ---------------------------------------------------------------- CLI surface

def test_cli_wake_tools_flag_parses():
    from galvanize import cli
    args = cli.build_parser().parse_args(
        ["add", "imap", "a@b.com", "--wake-tools", "terminal,file"])
    assert args.wake_tools == "terminal,file"


def _capture_add(monkeypatch, argv):
    from galvanize import cli
    captured = {}

    def fake_add(kind, target, **kw):
        captured.update(kw)
        return {"ok": False, "error": "stop after capture"}

    monkeypatch.setattr(cli.manage, "add_trigger", fake_add)
    cli.main(argv)
    return captured


def test_cli_add_splits_subset(isolated_homes, hermes_cfg, monkeypatch):
    got = _capture_add(monkeypatch,
                       ["add", "emit", "x", "--name", "cap",
                        "--wake-tools", "terminal, file ,"])
    assert got["wake_tools"] == ["terminal", "file"]


def test_cli_add_none_means_restricted(isolated_homes, hermes_cfg, monkeypatch):
    got = _capture_add(monkeypatch,
                       ["add", "emit", "x", "--name", "cap",
                        "--wake-tools", "none"])
    assert got["wake_tools"] == []


def test_cli_add_without_flag_passes_none(isolated_homes, hermes_cfg, monkeypatch):
    got = _capture_add(monkeypatch, ["add", "emit", "x", "--name", "cap"])
    assert got.get("wake_tools") is None


# ---------------------------------------------------------------- doctor rows

def _rows(r):
    return {c["name"]: c for c in r["checks"]}


def test_doctor_reports_parity_and_subset(isolated_homes, hermes_cfg):
    manage.add_trigger("emit", name="par", wake="hermes")
    manage.add_trigger("emit", name="sub", wake="hermes",
                       wake_tools=["terminal", "file"])
    rows = _rows(manage.doctor())
    p = rows.get("wake tools 'par'")
    assert p and p["ok"] and "parity" in p["note"] and not p.get("warn")
    s = rows.get("wake tools 'sub'")
    assert s and s["ok"] and "override: terminal, file" in s["note"]


def test_doctor_warns_on_legacy_keyless_route(isolated_homes, hermes_cfg):
    manage.add_trigger("emit", name="old", wake="hermes")
    subs_path = isolated_homes["hermes"] / "webhook_subscriptions.json"
    data = json.loads(subs_path.read_text())
    del data["old"]["toolsets"]  # simulate a route made by pre-v0.2.4
    subs_path.write_text(json.dumps(data))
    rows = _rows(manage.doctor())
    c = rows.get("wake tools 'old'")
    assert c and c["ok"] and c.get("warn"), c
    assert "fix" in c["note"].lower() and "doctor --fix" in c["note"]
    # An explicit restricted choice is NOT legacy drift: no warn.
    manage.add_trigger("emit", name="knob", wake="hermes", wake_tools=[])
    rows = _rows(manage.doctor())
    k = rows.get("wake tools 'knob'")
    assert k and k["ok"] and not k.get("warn") and "restricted" in k["note"]


# ---------------------------------------------------------------- --fix migration

def test_refresh_upgrades_legacy_restores_recorded_choices(isolated_homes, hermes_cfg):
    manage.add_trigger("emit", name="par", wake="hermes")            # parity
    manage.add_trigger("emit", name="sub", wake="hermes",
                       wake_tools=["terminal"])                      # recorded subset
    manage.add_trigger("emit", name="knob", wake="hermes",
                       wake_tools=[])                                # recorded restricted
    manage.add_trigger("emit", name="cust", wake="hermes")           # parity, unrecorded
    subs_path = isolated_homes["hermes"] / "webhook_subscriptions.json"
    data = json.loads(subs_path.read_text())
    del data["par"]["toolsets"]        # par goes "legacy" key-less
    data["sub"]["toolsets"] = ["web"]  # drift from the recorded choice
    data["cust"]["toolsets"] = ["file"]  # manual grant, no recording
    subs_path.write_text(json.dumps(data))

    r = manage.refresh_route_grants()
    assert r["ok"]
    assert {c["name"] for c in r["changed"]} == {"par", "sub"}
    data = json.loads(subs_path.read_text())
    assert data["par"]["toolsets"] == list(H.PARITY_TOOLSETS)  # legacy -> parity
    assert data["sub"]["toolsets"] == ["terminal"]             # recording wins
    assert "toolsets" not in data["knob"]                      # stays restricted
    assert data["cust"]["toolsets"] == ["file"]                # unrecorded grant kept
    # Idempotent: a second pass finds nothing.
    assert manage.refresh_route_grants()["changed"] == []


def test_refresh_dry_run_writes_nothing(isolated_homes, hermes_cfg):
    manage.add_trigger("emit", name="par", wake="hermes")
    subs_path = isolated_homes["hermes"] / "webhook_subscriptions.json"
    data = json.loads(subs_path.read_text())
    del data["par"]["toolsets"]
    subs_path.write_text(json.dumps(data))
    before = subs_path.read_text()
    r = manage.refresh_route_grants(dry_run=True)
    assert [c["name"] for c in r["changed"]] == ["par"]
    assert subs_path.read_text() == before


# ---------------------------------------------------------------- tool schemas

def test_plugin_schema_advertises_wake_tools():
    mod = _load_plugin_module()
    props = mod.ADD_SCHEMA["parameters"]["properties"]
    assert "wake_tools" in props
    assert props["wake_tools"]["type"] == "array"
    # Contract: the help must flag that granting tools to an autonomous
    # wake session is the user's call.
    assert "user" in mod.ADD_SCHEMA["description"].lower()
    # CLI fallback must forward the flag too.
    src = PLUGIN.read_text(encoding="utf-8")
    assert "--wake-tools" in src


def test_mcp_schema_advertises_wake_tools():
    from galvanize import mcp
    tool = next(t for t in mcp.TOOLS if t["name"] == "trigger_add")
    assert "wake_tools" in tool["inputSchema"]["properties"]


def test_version_bumped():
    from galvanize import __version__
    assert __version__ == "0.2.5"
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.2.5"' in text
    assert "0.2.5" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
