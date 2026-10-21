"""galvanize secrets import-himalaya + doctor drift check (card t_db3a57ee).

Zero network, zero real keyring: fixture TOML + the in-memory fake keyring
harness from test_secret_hardening. Asserts: key derivation matches the
add_trigger convention, dry-run purity, overwrite semantics, no password
values on stdout, and warn-not-fail drift behavior in doctor.
"""

import pytest

from galvanize import cli, himalaya, manage, secrets
from test_secret_hardening import install_fake_keyring

FIXTURE = """
[accounts.gmail]
email = "me@gmail.example"
[accounts.gmail.imap]
server = "imaps://imap.gmail.example:993"
[accounts.gmail.imap.sasl.plain]
username = "me@gmail.example"
password = { raw = "app-pw-gmail-99" }

[accounts.ktt]
email = "ops@ktt.example"
[accounts.ktt.imap]
server = "imaps://secure.emailsrvr.example:993"
[accounts.ktt.imap.sasl.plain]
username = "ops@ktt.example"
password = { raw = "ktt-app-pw-7" }

[accounts.cmdsecret]
email = "cmd@x.example"
[accounts.cmdsecret.imap]
server = "imaps://imap.x.example:993"
[accounts.cmdsecret.imap.sasl.plain]
username = "cmd@x.example"
password = { cmd = ["pass", "show", "mail"] }

[accounts.smtponly]
email = "send@only.example"
[accounts.smtponly.smtp]
server = "smtp://smtp.only.example:587"
"""

PW = {"gmail": "app-pw-gmail-99", "ktt": "ktt-app-pw-7"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setenv(himalaya.ENV_CONFIG, str(p))
    return p


@pytest.fixture
def vault(monkeypatch, cfg):
    """Fake keyring + fixture config; returns the call record."""
    return install_fake_keyring(monkeypatch)


def _item(res, user):
    return next(i for i in res["items"] if i["user"] == user)


# ------------------------------------------------------------- parsing

def test_parse_accounts_extracts_host_user_password_and_derives_key(cfg):
    accounts = {a.name: a for a in himalaya.parse_accounts(cfg)}
    assert set(accounts) == {"gmail", "ktt", "cmdsecret"}  # smtp-only skipped
    g = accounts["gmail"]
    assert (g.host, g.port, g.user) == ("imap.gmail.example", 993, "me@gmail.example")
    assert g.password == PW["gmail"]
    assert accounts["cmdsecret"].password is None  # cmd-store: no plaintext
    # key convention mirrors manage.add_trigger: f"imap:{user}"
    assert himalaya.secret_key(g.user) == "imap:me@gmail.example"


def test_missing_and_disabled_config_paths(tmp_path, monkeypatch):
    monkeypatch.setenv(himalaya.ENV_CONFIG, str(tmp_path / "nope.toml"))
    assert himalaya.config_path() is None
    r = himalaya.import_himalaya(dry_run=True)
    assert not r["ok"] and "no himalaya config" in r["error"]
    monkeypatch.setenv(himalaya.ENV_CONFIG, "")  # empty = explicitly disabled
    assert himalaya.config_path() is None


# --------------------------------------------------- key-convention lock

def test_imported_keys_match_add_trigger_keys(vault, cfg):
    """A mailbox added via manage.add_trigger must show 'unchanged' on
    import from the same himalaya password: proves both sides use one key."""
    r = manage.add_trigger("imap", "ops@ktt.example", wake="shell",
                           command="echo {event}", password=PW["ktt"],
                           imap_host="secure.emailsrvr.example")
    assert r["ok"], r
    res = himalaya.import_himalaya(dry_run=True)
    it = _item(res, "ops@ktt.example")
    assert it["action"] == "unchanged"
    assert it["used_by"] == ["ops"]  # trigger reference surfaced


# --------------------------------------------------------- dry-run purity

def test_dry_run_writes_nothing(vault):
    res = himalaya.import_himalaya(dry_run=True)
    assert res["ok"] and res["dry_run"]
    assert _item(res, "me@gmail.example")["action"] == "would import"
    assert _item(res, "cmd@x.example")["action"] == "skipped"
    imap_writes = [k for k in vault["sets"] if k.startswith("imap:")]
    assert imap_writes == []  # only the __probe__ key may have been written
    assert [k for k in vault["data"] if k[1].startswith("imap:")] == []
    assert himalaya.secret_key("me@gmail.example") not in secrets.list_keys()


# ------------------------------------------------------------- import path

def test_import_stores_values_indexes_keys_and_reruns_clean(vault):
    res = himalaya.import_himalaya()
    assert _item(res, "me@gmail.example")["action"] == "imported"
    assert _item(res, "ops@ktt.example")["action"] == "imported"
    # value actually landed, under the conventional key, and is enumerable
    assert vault["data"][("galvanize", "imap:me@gmail.example")] == PW["gmail"]
    assert "imap:me@gmail.example" in secrets.list_keys()
    # cmd-store account never wrote a secret
    assert himalaya.secret_key("cmd@x.example") not in secrets.list_keys()
    # second run is a no-op
    res2 = himalaya.import_himalaya()
    assert _item(res2, "me@gmail.example")["action"] == "unchanged"


# ------------------------------------------------------- overwrite semantics

def test_conflicting_secret_requires_overwrite(vault):
    secrets.set_secret("imap:me@gmail.example", "rotated-on-galvanize-side")
    res = himalaya.import_himalaya()
    it = _item(res, "me@gmail.example")
    assert it["action"] == "skipped" and "--overwrite" in it["reason"]
    assert secrets.get_secret("imap:me@gmail.example") == "rotated-on-galvanize-side"

    res2 = himalaya.import_himalaya(overwrite=True)
    assert _item(res2, "me@gmail.example")["action"] == "overwritten"
    assert secrets.get_secret("imap:me@gmail.example") == PW["gmail"]

    # dry-run against a differing secret says so without touching it
    secrets.set_secret("imap:me@gmail.example", "other")
    res3 = himalaya.import_himalaya(dry_run=True, overwrite=True)
    assert _item(res3, "me@gmail.example")["action"] == "would overwrite"
    assert secrets.get_secret("imap:me@gmail.example") == "other"


# --------------------------------------------------------- no value leakage

def test_cli_output_never_contains_passwords(vault, capsys):
    assert cli.main(["secrets", "import-himalaya", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for pw in PW.values():
        assert pw not in out
    assert "imap:me@gmail.example" in out  # keys/hosts shown, values not
    assert cli.main(["secrets", "import-himalaya"]) == 0
    out = capsys.readouterr().out
    for pw in PW.values():
        assert pw not in out


# ------------------------------------------------------------ drift check

def _drift():
    return {c["name"]: c for c in himalaya.drift_checks()}


def test_drift_missing_and_differing_warn_in_sync_passes(vault):
    d = _drift()
    assert d["himalaya 'gmail'"]["warn"] is True  # missing
    assert d["himalaya 'gmail'"]["ok"] is True    # warn never fails
    secrets.set_secret("imap:me@gmail.example", PW["gmail"])
    d = _drift()
    assert d["himalaya 'gmail'"]["ok"] and not d["himalaya 'gmail'"].get("warn")
    secrets.set_secret("imap:ops@ktt.example", "stale-value")
    d = _drift()
    assert d["himalaya 'ktt'"]["warn"] is True and "--overwrite" in d["himalaya 'ktt'"]["note"]
    # cmd-store account: reported as skipped, no warn
    assert d["himalaya 'cmdsecret'"]["ok"] and not d["himalaya 'cmdsecret'"].get("warn")


def test_doctor_surfaces_drift_without_failing(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(manage, "_hermes_config_has_mcp", lambda p: False)
    r = manage.doctor()
    hc = [c for c in r["checks"] if c["name"].startswith("himalaya")]
    assert hc and all(c["ok"] for c in hc)  # drift is warn, never ✖
    gmail = next(c for c in hc if c["name"] == "himalaya 'gmail'")
    assert gmail["warn"] is True and gmail["ok"] is True


def test_doctor_without_himalaya_config_is_quiet(vault, monkeypatch):
    monkeypatch.setenv(himalaya.ENV_CONFIG, "")
    r = manage.doctor()
    hc = [c for c in r["checks"] if c["name"] == "himalaya"]
    assert hc and hc[0]["ok"] and not hc[0].get("warn")
