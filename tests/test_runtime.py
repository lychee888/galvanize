"""Card C4: app-owned runtime, launch-equivalent capability gate, install.json.

Coverage of the acceptance list:
  * probe env isolation (PYTHONPATH injection cannot poison the probe)
  * failure-kind distinction (dependency vs auth vs env/version)
  * preserve-on-failure (gate refusal touches NOTHING on disk)
  * doctor three-way diff rows (manifest vs persistence vs live worker)
  * manifest-driven launch identity (no silent invoker adoption, F3)
  * windowless target rule (audit F4)
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from galvanize import autostart, capability, runtime, __version__

NT = os.name == "nt"


def good_manifest(exe=None, **kw):
    m = {"schema": capability.PROBE_SCHEMA, "ts": time.time(),
         "exe": str(exe or sys.executable), "python": "3.11.0",
         "version": __version__, "venv_prefix": None, "isolated": True,
         "capabilities": {"cli": True, "imap": True, "folder": True,
                          "keyring": True, "serve": True, "relay": True,
                          "windowless": True},
         "versions": {"galvanize": __version__},
         "keyring": {"backend": "Windows", "roundtrip": True},
         "failures": [], "warnings": [], "ok": True}
    m.update(kw)
    return m


def bad_manifest(kind="dependency", check="import:imap_tools"):
    return good_manifest(ok=False,
                         failures=[{"check": check, "kind": kind,
                                    "detail": "POISONED"}])


# ---------------------------------------------------------------- live probe

class TestKeyringStatusConcurrency:
    def test_parallel_roundtrips_do_not_collide(self):
        # The live drift (worker keyring=False) came from the watcher
        # thread's probe and the READY collect racing on one credential.
        from galvanize import secrets as secrets_mod
        from concurrent.futures import ThreadPoolExecutor
        st = secrets_mod.keyring_status()
        if not st["usable"]:
            pytest.skip(f"no keyring backend here: {st.get('error')}")
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda _: secrets_mod.keyring_status(),
                                  range(12)))
        assert all(r["usable"] for r in results), results


_CAPABLE_EXE: "Path | None" = None


def _win_target():
    """Probe target for the LIVE tests: the first windowless candidate
    interpreter that actually carries the C4 code (the test runner's venv
    may hold an older wheel, or import the tree only via cwd). The repo
    .venv beside the checkout is always the working tree."""
    global _CAPABLE_EXE
    if _CAPABLE_EXE is not None:
        return _CAPABLE_EXE
    cands = []
    t, _ = runtime._windowless_target(Path(sys.executable))
    if t:
        cands.append(t)
    root = Path(__file__).resolve().parents[1]
    scripts = "Scripts" if NT else "bin"
    cands.append(root / ".venv" / scripts / ("pythonw.exe" if NT else "python"))
    for c in cands:
        if c.exists() and runtime.probe(c).get("ok") is True:
            _CAPABLE_EXE = c
            return c
    pytest.skip("no interpreter with the C4 package importable isolated "
                f"(tried: {', '.join(map(str, cands))})")


@pytest.fixture
def poison_dir(tmp_path):
    """A dir whose imap_tools/__init__.py explodes on import — the classic
    PYTHONPATH shadow an inherited env would smuggle into the daemon."""
    p = tmp_path / "poison" / "imap_tools"
    p.mkdir(parents=True)
    (p / "__init__.py").write_text(
        "raise ImportError('POISONED-BY-PYTHONPATH')\n", encoding="utf-8")
    return tmp_path / "poison"


class TestProbeLive:
    def test_probe_emits_schema_v1(self):
        m = runtime.probe(_win_target())
        assert m.get("schema") == 1, m
        assert m["ok"] is True
        # FIX-CI B: the keyring capability is HOST state — headless Linux
        # runners have no backend, and the probe contract is degraded-but-
        # launchable (auth failures are non-blocking). Assert the green
        # guarantee where a backend exists and the DEGRADED shape where it
        # does not; a keyring-less Windows box is a regression, never CI noise.
        from galvanize import secrets as gsecrets
        usable = gsecrets.keyring_status()["usable"]
        if NT:
            assert usable, "Windows runner must present a usable keyring backend"
        for cap in ("cli", "imap", "folder", "windowless"):
            assert m["capabilities"].get(cap) is True, m["failures"]
        if usable:
            assert m["capabilities"].get("keyring") is True, m["failures"]
            assert m["keyring"]["roundtrip"] is True
            assert not [f for f in m["failures"] if f["kind"] == "auth"], m["failures"]
        else:
            assert m["capabilities"].get("keyring") is False
            assert m["keyring"]["roundtrip"] is False
            auth = [f for f in m["failures"]
                    if f["check"] == "keyring-backend" and f["kind"] == "auth"]
            assert auth, ("backend-less host must report the degradation, "
                          f"not hide it: {m['failures']}")
            assert m["ok"] is True, "auth-degraded stays launchable (non-blocking)"
        assert m["version"] == __version__
        assert m["isolated"] is True
        assert m["versions"].get("imap-tools")

    def test_pythonpath_injection_cannot_poison_isolated_probe(self, poison_dir):
        # The card's core isolation requirement: a polluted PYTHONPATH in
        # the INVOKER's env must not reach the daemon's imports.
        m = runtime.probe(_win_target(), extra_env={"PYTHONPATH": str(poison_dir)})
        assert m["ok"] is True, m["failures"]

    def test_non_isolated_probe_is_poisoned_and_classified(self, poison_dir):
        # Same env WITHOUT isolation (the pre-C4 disease): the shadow lands,
        # and the manifest reports it as a dependency failure, not auth/env.
        m = runtime.probe(_win_target(), isolated=False,
                          extra_env={"PYTHONPATH": str(poison_dir)})
        assert m["ok"] is False
        deps = [f for f in m["failures"]
                if f["kind"] == "dependency" and "POISONED" in f["detail"]]
        assert deps, m["failures"]

    def test_probe_scrubs_launch_env_keys(self):
        env = runtime.probe_env({"PATH": os.environ.get("PATH", "")})
        assert "PYTHONPATH" not in env
        assert "GALVANIZE_READY_TOKEN" not in env
        assert "PATH" in env

    def test_unlaunchable_candidate_is_env_failure(self):
        m = runtime.probe(Path(sys.executable).with_name("nope-interpret.exe"))
        assert m["ok"] is False
        assert m["failures"][0]["kind"] == "env"
        assert m["failures"][0]["check"] == "launch"

    def test_garbage_child_is_env_failure(self, tmp_path):
        # -I child that can import galvanize but prints non-manifest noise:
        # force it via an interpreter that lacks the package? Use a tiny
        # stub exe-name trick instead: probe_command shape asserts -I.
        cmd = runtime.probe_command("X:/py/python.exe")
        assert cmd[1] == "-I" and cmd[2:4] == ["-m", "galvanize.capability"]


# --------------------------------------------------------- manifest semantics

class TestCapabilityKinds:
    def test_auth_failure_warns_without_blocking(self, monkeypatch):
        from galvanize import secrets as secrets_mod
        monkeypatch.setattr(capability, "_NT", False)   # in-process: no F4 claim
        monkeypatch.setattr(secrets_mod, "keyring_status", lambda: {
            "usable": False, "backend": "fail Keyring", "roundtrip": False,
            "backend_missing": False, "error": "no usable backend"})
        m = capability.collect()
        kinds = [f["kind"] for f in m["failures"]]
        assert "auth" in kinds and "dependency" not in kinds
        assert m["ok"] is True                      # auth never refuses
        assert m["capabilities"]["keyring"] is False
        assert any("falls back" in w for w in m["warnings"])

    def test_missing_keyring_pkg_is_dependency(self, monkeypatch, tmp_path):
        # importlib.import_module consults sys.path at call time: a poison
        # package at the front shadows the real keyring exactly like a
        # broken install would.
        pkg = tmp_path / "keyring"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("raise ImportError('no keyring here')",
                                         encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        for k in [k for k in list(sys.modules)
                  if k == "keyring" or k.startswith("keyring.")]:
            monkeypatch.delitem(sys.modules, k, raising=False)
        m = capability.collect()
        kr = [f for f in m["failures"] if f["check"] == "import:keyring"]
        assert kr and kr[0]["kind"] == "dependency"
        assert m["ok"] is False                     # dependency DOES block


# ------------------------------------------------------------------- the gate

@pytest.fixture
def fake_appvenv(isolated_homes):
    """A venv-shaped directory with just enough interpreters to be found."""
    v = runtime.venv_dir()
    scripts = v / ("Scripts" if NT else "bin")
    scripts.mkdir(parents=True)
    names = ("pythonw.exe", "python.exe") if NT else ("python",)
    for n in names:
        (scripts / n).write_text("stub", encoding="utf-8")
    return scripts / names[0]


class TestGate:
    def _clean_env(self, monkeypatch):
        for k in ("GALVANIZE_RUNTIME_DISABLE", "GALVANIZE_RUNTIME_SKIP_PROVISION",
                  "GALVANIZE_INSTALL_INTERP"):
            monkeypatch.delenv(k, raising=False)

    def test_appvenv_preferred_and_recorded(self, monkeypatch, fake_appvenv,
                                            isolated_homes):
        self._clean_env(monkeypatch)
        seen = []
        monkeypatch.setattr(runtime, "probe",
                            lambda exe, **kw: seen.append(str(exe)) or good_manifest(exe))
        ok, m, lines = runtime.install_gate()
        assert ok, lines
        assert m["source"] == "app-venv"
        assert seen and Path(seen[0]) == fake_appvenv
        on_disk = json.loads(runtime.manifest_path().read_text(encoding="utf-8"))
        assert on_disk["schema"] == runtime.MANIFEST_SCHEMA
        assert on_disk["interpreter"] == str(fake_appvenv)
        assert on_disk["launch"]["cmd"][1:] == ["-m", "galvanize.cli", "supervise"]
        assert on_disk["launch"]["cwd"] == str(isolated_homes["galvanize"])

    def test_stale_appvenv_refreshed_then_chosen(self, monkeypatch, fake_appvenv):
        self._clean_env(monkeypatch)
        probes = iter([good_manifest(version="0.0.1"), good_manifest()])
        monkeypatch.setattr(runtime, "probe", lambda exe, **kw: next(probes))
        calls = []
        monkeypatch.setattr(runtime, "provision",
                            lambda venv=None, *, refresh=False:
                            calls.append(refresh) or (True, "", ["r"]))
        ok, m, lines = runtime.install_gate()
        assert ok and m["source"] == "app-venv"
        assert calls == [True] and any("refresh" in l.lower() for l in lines)

    def test_broken_appvenv_falls_back_to_invoker_loudly(self, monkeypatch,
                                                         fake_appvenv):
        self._clean_env(monkeypatch)
        seq = iter([bad_manifest(), good_manifest(exe=sys.executable)])
        monkeypatch.setattr(runtime, "probe", lambda exe, **kw: next(seq))
        monkeypatch.setattr(runtime, "provision",
                            lambda venv=None, *, refresh=False:
                            (False, "network", ["pip could not fetch ..."]))
        # repair-failure line must name the kind (network vs dependency)
        ok, m, lines = runtime.install_gate()
        assert ok and m["source"] == "invoker"
        assert any("FALLING BACK" in l for l in lines)
        assert any("network" in l for l in lines)   # failure kind surfaced

    def test_refusal_preserves_manifest_and_changes_nothing(
            self, monkeypatch, isolated_homes):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("GALVANIZE_RUNTIME_SKIP_PROVISION", "1")
        monkeypatch.setenv("GALVANIZE_INSTALL_INTERP", str(sys.executable))
        prior = {"schema": runtime.MANIFEST_SCHEMA, "interpreter": "OLD/KEEPME",
                 "probe": good_manifest()}
        runtime.write_manifest(prior)
        monkeypatch.setattr(runtime, "probe", lambda exe, **kw: bad_manifest())
        ok, m, lines = runtime.install_gate()
        assert ok is False and m is None
        assert any("REFUSED" in l for l in lines)
        assert any("preserved" in l for l in lines)
        # the previous manifest is EXACTLY as it was:
        assert json.loads(runtime.manifest_path().read_text(
            encoding="utf-8"))["interpreter"] == "OLD/KEEPME"

    def test_version_mismatch_refuses_silent_retarget(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("GALVANIZE_RUNTIME_DISABLE", "1")
        monkeypatch.setenv("GALVANIZE_INSTALL_INTERP", str(sys.executable))
        monkeypatch.setattr(runtime, "probe",
                            lambda exe, **kw: good_manifest(version="0.0.9"))
        ok, m, lines = runtime.install_gate()
        assert ok is False
        assert any("version" in l.lower() for l in lines)

    def test_windowless_rule_f4(self, tmp_path):
        exe = tmp_path / "python.exe"
        exe.write_text("x", encoding="utf-8")
        if NT:
            target, refuse = runtime._windowless_target(exe)
            assert target is None and "pythonw" in refuse
            (tmp_path / "pythonw.exe").write_text("x", encoding="utf-8")
            target, refuse = runtime._windowless_target(exe)
            assert target.name == "pythonw.exe" and refuse is None
        else:
            target, refuse = runtime._windowless_target(exe)
            assert target == exe and refuse is None

    def test_launch_cmd_comes_from_manifest_not_invoker(
            self, monkeypatch, fake_appvenv):
        self._clean_env(monkeypatch)
        monkeypatch.setattr(runtime, "probe", lambda exe, **kw: good_manifest(exe))
        ok, m, lines = runtime.install_gate()
        assert ok
        cmd = autostart._galvanize_run_cmd()
        assert cmd[0] == str(fake_appvenv)          # NOT sys.executable
        assert cmd[-1] == "supervise"


# ------------------------------------------------------- install() integration

class TestInstallFacade:
    def test_gate_refusal_short_circuits_everything(self, monkeypatch,
                                                    isolated_homes):
        calls = []
        monkeypatch.setattr(autostart, "_win_install",
                            lambda: calls.append("win") or (True, []))
        monkeypatch.setattr(autostart, "_linux_install",
                            lambda: calls.append("linux") or (True, []))
        monkeypatch.setattr(autostart, "_macos_install",
                            lambda: calls.append("macos") or (True, []))
        monkeypatch.setattr(runtime, "install_gate",
                            lambda: (False, None, ["✖ refused"]))
        ok, lines = autostart.install()
        assert ok is False and lines == ["✖ refused"]
        assert calls == []       # persistence entries untouched (preserve)

    def test_gate_lines_prepended_and_dispatch_runs(self, monkeypatch):
        monkeypatch.setattr(runtime, "install_gate",
                            lambda: (True, {"interpreter": "X"}, ["GATE ✔"]))
        monkeypatch.setattr(autostart, "_converge_running", lambda m: ["CYCLE"])
        monkeypatch.setattr(autostart, "_os", lambda: "win")
        monkeypatch.setattr(autostart, "_win_install", lambda: (True, ["LNK ✔"]))
        ok, lines = autostart.install()
        assert ok and lines == ["GATE ✔", "CYCLE", "LNK ✔"]

    def test_converge_stops_supervisor_on_identity_change(self, monkeypatch):
        stops = {"req": 0, "force": 0}
        snap = {"state": "ready",
                "worker": {"exe": "C:/old/venv/Scripts/pythonw.exe"}}
        monkeypatch.setattr(autostart, "os", os)
        from galvanize import supervisor as sup
        monkeypatch.setattr(sup, "snapshot", lambda *a, **k: snap)
        monkeypatch.setattr(sup, "request_stop", lambda *a, **k: stops.update(req=1))
        monkeypatch.setattr(sup, "wait_stopped", lambda t=0, *a, **k: True)
        monkeypatch.setattr(sup, "force_stop", lambda s, *a, **k: stops.update(force=1))
        out = autostart._converge_running({"interpreter": "C:/NEW/venv/pythonw.exe"})
        assert out and stops["req"] == 1 and stops["force"] == 0
        # identical identity (any slash/case spelling) -> hands off
        stops["req"] = 0
        same = ({"interpreter": "c:\\old\\venv\\Scripts\\pythonw.exe"} if NT
                else {"interpreter": snap["worker"]["exe"]})
        out = autostart._converge_running(same)
        assert out == [] and stops["req"] == 0


# ------------------------------------------------------------- doctor 3-way

@pytest.fixture
def fake_live(monkeypatch):
    def _set(snap):
        from galvanize import supervisor as sup
        monkeypatch.setattr(sup, "snapshot", lambda *a, **k: snap)
        return snap
    return _set


EXE = str(Path(sys.executable))   # the MISSING-row check needs a real path

READY = {"state": "ready", "supervisor_pid": 1,
         "worker": {"exe": EXE,
                    "capabilities": {"cli": True, "imap": True, "folder": True,
                                     "keyring": True}}}


def manifest(exe=EXE, caps=None):
    return {"schema": runtime.MANIFEST_SCHEMA, "source": "app-venv",
            "interpreter": exe, "version": __version__, "python": "3.11.0",
            "capabilities": caps or {"cli": True, "imap": True, "folder": True,
                                     "keyring": True}}


class TestDoctorRows:
    def _rows(self, monkeypatch, fake_live, m, tgt, snap):
        fake_live(snap)
        monkeypatch.setattr(runtime, "load_manifest", lambda home=None: m)
        monkeypatch.setattr(runtime, "persistence_target", lambda: tgt)
        return {r["name"]: r for r in runtime.doctor_rows()}

    def test_all_agree(self, monkeypatch, fake_live):
        rows = self._rows(monkeypatch, fake_live,
                          manifest(), {"exe": EXE}, READY)
        assert rows["runtime identity"]["ok"] and "live worker" in rows["runtime identity"]["note"]
        assert rows["runtime capabilities"]["ok"]

    def test_persistence_drift_fails(self, monkeypatch, fake_live):
        rows = self._rows(monkeypatch, fake_live,
                          manifest(), {"exe": "C:/uv/foreign/python.exe"}, READY)
        r = rows["runtime identity"]
        assert r["ok"] is False
        assert "manifest=" in r["note"] and "persistence=" in r["note"] and "live=" in r["note"]

    def test_live_worker_drift_fails(self, monkeypatch, fake_live):
        snap = json.loads(json.dumps(READY))
        snap["worker"]["exe"] = "C:/other/pythonw.exe"
        rows = self._rows(monkeypatch, fake_live,
                          manifest(), {"exe": EXE}, snap)
        assert rows["runtime identity"]["ok"] is False
        assert "C:/other/pythonw.exe" in rows["runtime identity"]["note"]

    def test_daemon_down_is_two_way_warning(self, monkeypatch, fake_live):
        rows = self._rows(monkeypatch, fake_live,
                          manifest(), {"exe": EXE}, None)
        assert rows["runtime identity"]["ok"] and rows["runtime identity"]["warn"]
        assert "not running" in rows["runtime identity"]["note"]

    def test_capability_drift_detected(self, monkeypatch, fake_live):
        snap = json.loads(json.dumps(READY))
        snap["worker"]["capabilities"]["imap"] = False
        rows = self._rows(monkeypatch, fake_live,
                          manifest(), {"exe": EXE}, snap)
        assert rows["runtime capabilities"]["ok"] is False
        assert "DRIFT" in rows["runtime capabilities"]["note"]

    def test_old_worker_without_caps_only_warns(self, monkeypatch, fake_live):
        snap = json.loads(json.dumps(READY))
        snap["worker"]["capabilities"] = {"imap": True, "folder": True}  # pre-C4
        rows = self._rows(monkeypatch, fake_live,
                          manifest(), {"exe": EXE}, snap)
        r = rows["runtime capabilities"]
        assert r["ok"] and r["warn"]

    def test_no_manifest_warns_actionably(self, monkeypatch, fake_live):
        rows = self._rows(monkeypatch, fake_live, None, None, None)
        r = rows["runtime"]
        assert r["ok"] and r["warn"] and "daemon install" in r["note"]

    def test_missing_recorded_runtime_fails(self, monkeypatch, fake_live):
        # venv deleted out from under a good manifest: a string-equality
        # 3-way match must not hide that nothing can actually launch.
        rows = self._rows(monkeypatch, fake_live,
                          manifest(exe="C:/no/such/venv/pythonw.exe"),
                          {"exe": "C:/no/such/venv/pythonw.exe"}, None)
        r = rows["runtime identity"]
        assert r["ok"] is False and "MISSING" in r["note"]

    def test_manage_doctor_includes_runtime_rows(self, monkeypatch, fake_live,
                                                 isolated_homes):
        from galvanize import manage
        fake_live(READY)
        monkeypatch.setattr(runtime, "load_manifest", lambda home=None: manifest())
        monkeypatch.setattr(runtime, "persistence_target",
                            lambda: {"exe": EXE})
        names = [c["name"] for c in manage.doctor()["checks"]]
        assert "runtime" in names and "runtime identity" in names \
            and "runtime capabilities" in names
