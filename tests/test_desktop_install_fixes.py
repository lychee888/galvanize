"""Post-reinstall fix batch (2026-10-09): desktop-interpreter resolution,
CLI-verb isolated-launcher fallback, emit-not-a-watcher cosmetic mark."""

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import galvanize.cli as cli
import galvanize.manage as manage
import galvanize.state as state
from galvanize.config import Trigger, upsert_trigger


# ------------------------------------------------ gateway interpreter probe

def test_gateway_interpreter_reads_pid_from_gateway_state(isolated_homes):
    gh = isolated_homes["hermes"]
    exe = Path(sys.executable)
    (gh / "gateway_state.json").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    with patch("galvanize.cli._proc_exe", return_value=exe) as px:
        got = cli._gateway_interpreter()
    px.assert_called_once_with(4242)
    assert got == exe


@pytest.mark.parametrize("raw", ["", "{broken", '{"pid": 0}', '{}'])
def test_gateway_interpreter_fails_closed(isolated_homes, raw):
    gh = isolated_homes["hermes"]
    if raw:
        (gh / "gateway_state.json").write_text(raw, encoding="utf-8")
    with patch("galvanize.cli._proc_exe", return_value=Path(sys.executable)):
        assert cli._gateway_interpreter() is None


def test_hermes_interpreters_prefers_gateway_and_dedupes(isolated_homes):
    gw = Path(sys.executable)
    other = Path(sys.executable).parent / ("pythonX" + ("_.exe" if sys.platform == "win32" else "_"))
    other.touch()
    with patch("galvanize.cli._gateway_interpreter", return_value=gw), \
         patch("galvanize.cli._path_interpreter", return_value=gw):
        assert cli._hermes_interpreters() == [gw]           # dedupe by resolve()
    with patch("galvanize.cli._gateway_interpreter", return_value=gw), \
         patch("galvanize.cli._path_interpreter", return_value=other):
        assert cli._hermes_interpreters() == [gw, other]    # gateway first
    with patch("galvanize.cli._gateway_interpreter", return_value=None), \
         patch("galvanize.cli._path_interpreter", return_value=other):
        assert cli._hermes_interpreters() == [other]        # PATH fallback alone
    with patch("galvanize.cli._gateway_interpreter", return_value=None), \
         patch("galvanize.cli._path_interpreter", return_value=None):
        assert cli._hermes_interpreters() == []


def test_ensure_installs_into_every_interpreter_missing_the_pkg(tmp_path):
    runs = []

    def fake_run(cmd, **kw):
        runs.append(list(cmd))
        interp = Path(cmd[0])
        ok = "good" in str(interp.parent)

        class R:
            returncode = 0 if ok else 1
            stdout = stderr = ""
        return R()

    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good.mkdir(), bad.mkdir()
    with patch("galvanize.cli._hermes_interpreters",
               return_value=[good / "python.exe", bad / "python.exe"]), \
         patch("subprocess.run", side_effect=fake_run):
        cli._ensure_galvanize_in_hermes_venv(quiet=True)
    probes = [r for r in runs if "-c" in r]
    installs = [r for r in runs if "install" in r]
    assert len(probes) == 2                      # both interpreters probed
    assert len(installs) == 1                    # only the failing one installed
    assert installs[0][0] == str(bad / "python.exe")


# ------------------------------------------------- emit rows are 'manual'

def test_status_marks_emit_triggers_manual_not_error():
    upsert_trigger(Trigger("ping", {"type": "emit"}, {"kind": "log"}))
    state.set_heartbeat()
    row = manage.status()["triggers"][0]
    assert row.get("manual") is True and not row["watching"]


def test_status_watcher_rows_have_no_manual_flag():
    upsert_trigger(Trigger("foldy", {"type": "folder", "path": "x"}, {"kind": "log"}))
    state.set_heartbeat()
    row = manage.status()["triggers"][0]
    assert "manual" not in row


# ------------------------------------- plugin /triggers + CLI-verb fallback

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "hermes" / "__init__.py"


def _load_plugin_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gz_hermes_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_verb_falls_back_to_shim_when_not_importable(monkeypatch, capsys):
    """hermes.exe runs the CLI with -I -S: site-packages invisible, shim on PATH."""
    mod = _load_plugin_module()
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd

        class P:
            returncode = 0
            stdout = "daemon: up (shim fallback ran)"
            stderr = ""
        return P()

    real_import = __import__

    def blocked_import(name, *a, **k):
        if name == "galvanize" or name.startswith("galvanize."):
            raise ImportError("blocked: isolated launcher -S mode")
        return real_import(name, *a, **k)

    monkeypatch.setattr("shutil.which",
                        lambda n: r"C:\py\Scripts\galvanize.exe" if n.startswith("galvanize") else None)
    monkeypatch.setattr("subprocess.run", fake_run)
    args = types.SimpleNamespace(verb="status", name="")
    import builtins
    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(SystemExit) as e:
        mod._cli_handle_triggers(args)
    assert e.value.code == 0
    assert "shim fallback" in capsys.readouterr().out
    assert calls["cmd"][0].endswith("galvanize.exe") and calls["cmd"][1] == "status"


def test_cli_verb_fallback_maps_doctor_verb_to_shim(monkeypatch, capsys):
    """The shim argv must carry the verb: doctor->doctor, list->status."""
    mod = _load_plugin_module()
    seen = []

    def fake_run(cmd, **kw):
        seen.append(list(cmd))

        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return P()

    real_import = __import__

    def blocked_import(name, *a, **k):
        if name == "galvanize" or name.startswith("galvanize."):
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr("shutil.which",
                        lambda n: r"C:\py\Scripts\galvanize.exe" if n.startswith("galvanize") else None)
    monkeypatch.setattr("subprocess.run", fake_run)
    import builtins
    monkeypatch.setattr(builtins, "__import__", blocked_import)
    for verb, expect in (("doctor", "doctor"), ("list", "status"), ("status", "status")):
        seen.clear()
        with pytest.raises(SystemExit):
            mod._cli_handle_triggers(types.SimpleNamespace(verb=verb, name=""))
        assert seen[0][1] == expect, f"verb {verb} mapped to {seen[0][1]}"


def _fake_manage_with_manual(status_rows):
    mod = types.ModuleType("galvanize.manage")
    mod.status = lambda: {"daemon_alive": True, "webhook_enabled": True,
                          "gateway_running": True, "triggers": status_rows,
                          "notes": [], "relay_failures": []}
    return mod


def test_plugin_renderers_show_manual_mark_for_emit(monkeypatch, capsys):
    rows = [
        {"name": "imap-one", "source": "imap", "wake": "hermes", "enabled": True,
         "watching": True, "manual": False, "last_fire": "now", "fires_today": 0,
         "last_error": None},
        {"name": "emit-one", "source": "emit", "wake": "hermes", "enabled": True,
         "watching": False, "manual": True, "last_fire": "now", "fires_today": 2,
         "last_error": None},
    ]
    fake = _fake_manage_with_manual(rows)
    pkg = types.ModuleType("galvanize")
    pkg.manage = fake
    monkeypatch.setitem(sys.modules, "galvanize", pkg)
    monkeypatch.setitem(sys.modules, "galvanize.manage", fake)
    mod = _load_plugin_module()
    out = mod._slash_triggers("status")
    assert "● imap-one" in out and "⇢ emit-one" in out
    mod._cli_handle_triggers(types.SimpleNamespace(verb="status", name=""))
    printed = capsys.readouterr().out
    assert "● imap-one" in printed and "⇢ emit-one" in printed


def test_cli_verb_survives_without_shim(monkeypatch, capsys):
    """No package AND no shim: actionable fix hint with this interpreter, exit 1."""
    mod = _load_plugin_module()
    import builtins
    real_import = __import__

    def blocked_import(name, *a, **k):
        if name == "galvanize" or name.startswith("galvanize."):
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.setattr("shutil.which", lambda n: None)
    args = types.SimpleNamespace(verb="status", name="")
    with pytest.raises(SystemExit) as e:
        mod._cli_handle_triggers(args)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert sys.executable in out and "pip install galvanize" in out


def test_slash_triggers_hint_names_the_interpreter(monkeypatch):
    """The /triggers chat command must tell the user WHICH python to install into."""
    mod = _load_plugin_module()
    import builtins
    real_import = __import__

    def blocked_import(name, *a, **k):
        if name == "galvanize" or name.startswith("galvanize."):
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    out = mod._slash_triggers("status")
    assert sys.executable in out and "galvanize init" in out
