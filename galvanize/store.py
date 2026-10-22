"""SQLite: the single transactional state authority (card C5, audit F8/F9).

Everything galvanize mutates lives here — trigger config (revisioned),
per-trigger fire/error state, the daemon heartbeat, source health, imap
resume anchors, and webhook route INTENT. The old flat files (state.json,
imap_state.json, source-*.json) are import-once artifacts; triggers.yaml is
demoted to import/export + first-seed (see config.py).

Transaction discipline (judge verdict #3):
  - WAL mode + bounded busy_timeout; every op opens a short-lived conn.
  - Compound mutations run under BEGIN IMMEDIATE so two processes (CLI +
    worker + dashboard in-process plugin, all offline, no daemon IPC) get
    real serialization, not last-writer-wins file clobbers.
  - NO network calls inside a transaction. Route writes to the gateway's
    webhook_subscriptions.json serialize through here (hermes.py) because
    that write is local disk; anything HTTP stays outside.
  - Heartbeat and fire history are separate UPSERT/INSERT rows — the old
    read-modify-write of one whole state.json lost updates between the
    main loop and dispatch threads by construction.

Config revisions: meta.cfg_rev increments on every activated change. An
editor that read revision N and writes with expected_revision=N loses
loudly (StaleRevisionError -> 409 on serve, nonzero on CLI) instead of
silently clobbering whatever landed in between. Corruption of an import
source keeps the committed last-known-good rows and records a loud
config_state the doctor surfaces (UNHEALTHY, never "no triggers").
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .paths import ensure_home, galvanize_home

logger = logging.getLogger("galvanize.store")

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000          # bounded: writers queue, they do not hang
FIRE_HISTORY_KEEP = 5000        # global row cap; oldest trimmed on insert
DB_NAME = "galvanize.db"

_schema_lock = threading.Lock()
_ready_done: set[str] = set()    # abs db paths migrated this process
_ready_busy: set[str] = set()    # re-entrancy guard (migration reads its own db)


class StaleRevisionError(RuntimeError):
    """Config write attempted against an out-of-date revision."""


def db_path() -> Path:
    return ensure_home() / DB_NAME


# ------------------------------------------------------------------ plumbing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trigger_config (
    name TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trigger_state (
    name TEXT PRIMARY KEY,
    last_fire REAL, last_error TEXT, last_error_at REAL,
    fires_date TEXT, fires_today INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fire_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL, ts REAL NOT NULL,
    ok INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_health (
    key TEXT PRIMARY KEY, connected INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT '', ts REAL NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS imap_state (
    name TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL DEFAULT '',
    last_uid INTEGER NOT NULL DEFAULT 0, seen TEXT NOT NULL DEFAULT '[]',
    anchored_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS route_intent (
    name TEXT PRIMARY KEY, route TEXT NOT NULL, updated_at REAL NOT NULL);
"""


def connect() -> sqlite3.Connection:
    """Short-lived connection with the WAL/timeout policy applied.

    The one chokepoint every statement flows through — so it is also where
    the lazy first-run migration happens (re-entrancy-guarded: the
    migration's own writes come back through here and skip).
    """
    ensure_ready()
    path = db_path()
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    # Flaky-C5-test root cause (card C6 release gate): the journal-mode
    # flip is the ONE statement documented NOT to invoke the busy handler
    # — a concurrent write transaction makes it return SQLITE_BUSY
    # immediately. It only ever contends before the header already says
    # WAL, so a bounded retry is the whole fix (once WAL, it is lock-free
    # and returns instantly).
    deadline = time.time() + BUSY_TIMEOUT_MS / 1000.0
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or time.time() >= deadline:
                raise
            time.sleep(0.02)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    with _schema_lock:
        conn.executescript(_SCHEMA)
    return conn


class _Tx:
    """Context manager: BEGIN IMMEDIATE -> yield conn -> commit/rollback."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()
        return False


def write_tx() -> _Tx:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    return _Tx(conn)


def read(fn: Callable[[sqlite3.Connection], Any]) -> Any:
    conn = connect()
    try:
        return fn(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------- meta

def _meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_get(key: str, default: str = "") -> str:
    return read(lambda c: _meta_get(c, key, default))


def _meta_set(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


def meta_set(key: str, value: object) -> None:
    with write_tx() as conn:
        _meta_set(conn, key, value)


# ------------------------------------------------------------- config store

def current_revision(conn_or_none=None) -> int:
    if conn_or_none is not None:
        return int(_meta_get(conn_or_none, "cfg_rev", "0") or 0)
    return read(lambda c: int(_meta_get(c, "cfg_rev", "0") or 0))


def load_config_bodies(conn: sqlite3.Connection) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for row in conn.execute("SELECT name, body FROM trigger_config"):
        try:
            out[row[0]] = json.loads(row[1])
        except ValueError:
            logger.error("trigger_config row '%s' has invalid JSON body", row[0])
    return out


def config_bodies() -> Dict[str, dict]:
    return read(load_config_bodies)


def config_state() -> Tuple[str, str, int]:
    """(state, note, revision) — state: 'ok' | 'corrupt' | 'never'."""
    def _q(conn):
        st = _meta_get(conn, "config_state", "")
        note = _meta_get(conn, "config_note", "")
        rev = int(_meta_get(conn, "cfg_rev", "0") or 0)
        return st, note, rev
    st, note, rev = read(_q)
    if not st:
        st = "ok" if rev else "never"
    return st, note, rev


def _set_config_state(conn: sqlite3.Connection, state: str, note: str) -> None:
    _meta_set(conn, "config_state", state)
    _meta_set(conn, "config_note", note[:500])


def note_config_state(state: str, note: str = "") -> None:
    """Record the config activation outcome WITHOUT changing rows: a failed
    import stays loud in doctor while the committed revision keeps serving."""
    with write_tx() as conn:
        _set_config_state(conn, state, note)


def mutate_config(fn: Callable[[Dict[str, dict]], None],
                  *, expected_revision: Optional[int] = None,
                  note: str = "mutation") -> int:
    """Read-modify-write the whole trigger set under BEGIN IMMEDIATE.

    `fn` mutates the {name: body-dict} mapping in place. With
    expected_revision set, a write against a moved revision raises
    StaleRevisionError and changes NOTHING (no partial activation).
    Returns the new revision.
    """
    with write_tx() as conn:
        rev = int(_meta_get(conn, "cfg_rev", "0") or 0)
        if expected_revision is not None and expected_revision != rev:
            raise StaleRevisionError(
                f"config revision moved: editor held {expected_revision}, "
                f"authority is {rev} — re-read and retry ({note})")
        bodies = load_config_bodies(conn)
        fn(bodies)
        now = time.time()
        conn.execute("DELETE FROM trigger_config")
        for name, body in bodies.items():
            conn.execute(
                "INSERT INTO trigger_config(name, body, updated_at) VALUES (?,?,?)",
                (name, json.dumps(body, sort_keys=True), now))
        rev += 1
        _meta_set(conn, "cfg_rev", rev)
        _set_config_state(conn, "ok", note)
    return rev


def replace_config(bodies: Dict[str, dict], *,
                   expected_revision: Optional[int] = None,
                   note: str = "replace") -> int:
    def _fn(cur: Dict[str, dict]) -> None:
        cur.clear()
        cur.update(bodies)
    return mutate_config(_fn, expected_revision=expected_revision, note=note)


def upsert_body(name: str, body: dict, *,
                expected_revision: Optional[int] = None,
                note: str = "") -> int:
    def _fn(cur: Dict[str, dict]) -> None:
        cur[name] = body
    return mutate_config(_fn, expected_revision=expected_revision,
                         note=note or f"upsert {name}")


def delete_body(name: str, *, expected_revision: Optional[int] = None,
                note: str = "") -> int:
    def _fn(cur: Dict[str, dict]) -> None:
        cur.pop(name, None)
    return mutate_config(_fn, expected_revision=expected_revision,
                         note=note or f"delete {name}")


# -------------------------------------------------------------- run state

def _record_fire_sql(conn: sqlite3.Connection, name: str, ok: bool,
                     detail: str, now: float) -> None:
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if ok:
        # Atomic UPSERT-with-increment: no read-modify-write window, so two
        # dispatcher threads can never lose a fire to each other.
        conn.execute(
            """INSERT INTO trigger_state(name, last_fire, fires_date, fires_today,
                                         last_error, last_error_at)
               VALUES(?,?,?,1,NULL,NULL)
               ON CONFLICT(name) DO UPDATE SET
                 last_fire=excluded.last_fire,
                 fires_date=excluded.fires_date,
                 fires_today=CASE WHEN trigger_state.fires_date=excluded.fires_date
                                  THEN trigger_state.fires_today+1 ELSE 1 END,
                 last_error=NULL, last_error_at=NULL""",
            (name, now, today))
    else:
        conn.execute(
            """INSERT INTO trigger_state(name, last_error, last_error_at)
               VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 last_error=excluded.last_error,
                 last_error_at=excluded.last_error_at""",
            (name, detail[:400], now))
    conn.execute("INSERT INTO fire_history(trigger, ts, ok, detail) VALUES(?,?,?,?)",
                 (name, now, 1 if ok else 0, detail[:400]))
    conn.execute(
        "DELETE FROM fire_history WHERE id < "
        "(SELECT COALESCE(MAX(id),0) - ? FROM fire_history)", (FIRE_HISTORY_KEEP,))


def record_fire(name: str, ok: bool, detail: str = "", now: Optional[float] = None) -> None:
    with write_tx() as conn:
        _record_fire_sql(conn, name, ok, detail, time.time() if now is None else now)


def trigger_state(name: str) -> Dict[str, Any]:
    def _q(conn):
        row = conn.execute("SELECT * FROM trigger_state WHERE name=?", (name,)).fetchone()
        if not row:
            return {}
        out = dict(row)
        return {k: v for k, v in out.items()
                if v is not None and k != "name"}
    return read(_q)


def all_trigger_state() -> Dict[str, dict]:
    def _q(conn):
        return {row["name"]: {k: v for k, v in dict(row).items()
                              if v is not None and k != "name"}
                for row in conn.execute("SELECT * FROM trigger_state")}
    return read(_q)


def fire_history(limit: int = 50, trigger: Optional[str] = None) -> List[dict]:
    def _q(conn):
        if trigger:
            rows = conn.execute(
                "SELECT trigger, ts, ok, detail FROM fire_history "
                "WHERE trigger=? ORDER BY id DESC LIMIT ?", (trigger, limit))
        else:
            rows = conn.execute(
                "SELECT trigger, ts, ok, detail FROM fire_history "
                "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
    return read(_q)


# -------------------------------------------------------------- heartbeat

def set_heartbeat(pid: int, ts: Optional[float] = None) -> None:
    with write_tx() as conn:
        conn.execute("INSERT OR REPLACE INTO heartbeat(id, pid, ts) VALUES(1,?,?)",
                     (int(pid), time.time() if ts is None else ts))


def clear_heartbeat() -> None:
    with write_tx() as conn:
        conn.execute("DELETE FROM heartbeat")


def get_heartbeat() -> Optional[dict]:
    def _q(conn):
        row = conn.execute("SELECT pid, ts FROM heartbeat WHERE id=1").fetchone()
        return {"pid": row[0], "heartbeat": row[1]} if row else None
    return read(_q)


# ------------------------------------------------------------ source health

def set_source_health(key: str, connected: bool, error: str,
                      pid: Optional[int] = None, ts: Optional[float] = None) -> None:
    with write_tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO source_health(key, connected, error, ts, pid) "
            "VALUES(?,?,?,?,?)",
            (key, 1 if connected else 0, error[:500],
             time.time() if ts is None else ts,
             int(os.getpid() if pid is None else pid)))


def source_health_row(key: str) -> Optional[dict]:
    def _q(conn):
        row = conn.execute("SELECT * FROM source_health WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        d = dict(row)
        return {"connected": bool(d["connected"]), "error": d["error"],
                "ts": d["ts"], "pid": d["pid"]}
    return read(_q)


# ---------------------------------------------------------------- imap state

def imap_get(name: str) -> Dict[str, Any]:
    def _q(conn):
        row = conn.execute("SELECT * FROM imap_state WHERE name=?", (name,)).fetchone()
        if not row:
            return {}
        d = dict(row)
        try:
            seen = json.loads(d.get("seen") or "[]")
        except ValueError:
            seen = []
        return {"uidvalidity": d["uidvalidity"], "last_uid": d["last_uid"],
                "seen": seen if isinstance(seen, list) else [],
                "anchored_at": d["anchored_at"]}
    return read(_q)


def imap_put(name: str, value: dict) -> None:
    try:
        last_uid = int(value.get("last_uid", 0) or 0)
    except (TypeError, ValueError):
        last_uid = 0
    with write_tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO imap_state(name, uidvalidity, last_uid, seen, anchored_at) "
            "VALUES(?,?,?,?,?)",
            (name, str(value.get("uidvalidity", "")), last_uid,
             json.dumps(list(value.get("seen") or [])),
             float(value.get("anchored_at", 0) or 0)))


# ----------------------------------------------------------- route intent

def _intent_upsert(conn: sqlite3.Connection, name: str, route: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO route_intent(name, route, updated_at) VALUES(?,?,?)",
        (name, json.dumps(route, sort_keys=True), time.time()))


def route_intent_set(name: str, route: dict) -> None:
    with write_tx() as conn:
        _intent_upsert(conn, name, route)


def route_intent_delete(name: str) -> None:
    with write_tx() as conn:
        conn.execute("DELETE FROM route_intent WHERE name=?", (name,))


def route_intents() -> Dict[str, dict]:
    return read(_route_intents_sql)


def _route_intents_sql(conn: sqlite3.Connection) -> Dict[str, dict]:
    out = {}
    for row in conn.execute("SELECT name, route FROM route_intent"):
        try:
            out[row["name"]] = json.loads(row["route"])
        except ValueError:
            continue
    return out


# --------------------------------------------------------------- migration

def migrated(conn: sqlite3.Connection) -> bool:
    return bool(_meta_get(conn, "migrated_at", ""))


def _seed_config_from_yaml(conn: sqlite3.Connection, home: Path) -> None:
    """First-seed: activate triggers.yaml into the authority as revision 1.

    Structural failure (unparseable YAML, bad source/wake shape) => loud
    'corrupt' state, NO rows activated. A folder path that merely doesn't
    exist right now is a warning — doctor already surfaces it per trigger.
    """
    import yaml

    from .config import Trigger
    p = home / "triggers.yaml"
    if not p.exists():
        _set_config_state(conn, "never", "no triggers.yaml and no activated config")
        return
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _set_config_state(conn, "corrupt", f"triggers.yaml unparseable: {e}")
        logger.error("first-seed: triggers.yaml is corrupt: %s", e)
        return
    raw = data.get("triggers") if isinstance(data, dict) else None
    if raw is None:
        _set_config_state(conn, "never", "triggers.yaml has no 'triggers:' mapping")
        return
    if not isinstance(raw, dict):
        _set_config_state(conn, "corrupt", "triggers.yaml 'triggers:' is not a mapping")
        return
    bodies: Dict[str, dict] = {}
    hard: List[str] = []
    for name, td in raw.items():
        name = str(name)
        if not isinstance(td, dict):
            hard.append(f"{name}: not a mapping")
            continue
        t = Trigger.from_dict(name, td)
        errs = [e for e in t.validate() if "folder path does not exist" not in e]
        if errs:
            hard.append(f"{name}: {'; '.join(errs)}")
            continue
        bodies[name] = t.to_dict()
    if hard:
        _set_config_state(conn, "corrupt", "invalid triggers: " + " | ".join(hard))
        logger.error("first-seed: triggers.yaml invalid, config NOT activated: %s",
                     " | ".join(hard))
        return
    now = time.time()
    for name, body in bodies.items():
        conn.execute(
            "INSERT INTO trigger_config(name, body, updated_at) VALUES(?,?,?)",
            (name, json.dumps(body, sort_keys=True), now))
    _meta_set(conn, "cfg_rev", 1)
    _set_config_state(conn, "ok", "first-seed from triggers.yaml")


def _import_state_json(conn: sqlite3.Connection, home: Path) -> int:
    import contextlib
    p = home / "state.json"
    if not p.exists():
        return 0
    with contextlib.suppress(Exception):
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key, val in data.items():
                if not isinstance(val, dict):
                    continue
                if key == "_daemon":
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO heartbeat(id, pid, ts) VALUES(1,?,?)",
                            (int(val.get("pid") or 0), float(val.get("heartbeat") or 0)))
                    except (TypeError, ValueError):
                        pass
                    continue
                try:
                    fires_today = int(val.get("fires_today", 0) or 0)
                except (TypeError, ValueError):
                    fires_today = 0
                conn.execute(
                    "INSERT OR REPLACE INTO trigger_state"
                    "(name, last_fire, last_error, last_error_at, fires_date, fires_today) "
                    "VALUES(?,?,?,?,?,?)",
                    (key,
                     float(val["last_fire"]) if val.get("last_fire") else None,
                     (str(val["last_error"])[:400] if val.get("last_error") else None),
                     float(val["last_error_at"]) if val.get("last_error_at") else None,
                     str(val.get("fires_date") or "") or None,
                     fires_today))
            return len(data)
    return 0


def _import_imap_json(conn: sqlite3.Connection, home: Path) -> int:
    import contextlib
    p = home / "imap_state.json"
    if not p.exists():
        return 0
    n = 0
    with contextlib.suppress(Exception):
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for name, val in data.items():
                if isinstance(val, dict):
                    imap_put_sql(conn, str(name), val)
                    n += 1
    return n


def imap_put_sql(conn: sqlite3.Connection, name: str, value: dict) -> None:
    """imap_put for use INSIDE an existing transaction."""
    try:
        last_uid = int(value.get("last_uid", 0) or 0)
    except (TypeError, ValueError):
        last_uid = 0
    conn.execute(
        "INSERT OR REPLACE INTO imap_state(name, uidvalidity, last_uid, seen, anchored_at) "
        "VALUES(?,?,?,?,?)",
        (name, str(value.get("uidvalidity", "")), last_uid,
         json.dumps(list(value.get("seen") or [])),
         float(value.get("anchored_at", 0) or 0)))


def _import_source_health(conn: sqlite3.Connection, home: Path) -> int:
    import contextlib
    n = 0
    for p in sorted(home.glob("source-*.json")):
        with contextlib.suppress(Exception):
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                continue
            key = p.stem[len("source-"):]
            conn.execute(
                "INSERT OR REPLACE INTO source_health(key, connected, error, ts, pid) "
                "VALUES(?,?,?,?,?)",
                ("legacy:" + key, 1 if d.get("connected") else 0,
                 str(d.get("error") or "")[:500],
                 float(d.get("ts") or 0), int(d.get("pid") or 0)))
            n += 1
    return n


def _import_route_intent(conn: sqlite3.Connection) -> int:
    import contextlib
    n = 0
    try:
        from . import hermes as hermes_mod
        p = hermes_mod._subs_path()
        if not p.exists():
            return 0
        with contextlib.suppress(Exception):
            subs = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(subs, dict):
                for name, route in subs.items():
                    if isinstance(route, dict) and \
                            route.get("managed_by") == hermes_mod.MANAGED_BY:
                        conn.execute(
                            "INSERT OR REPLACE INTO route_intent(name, route, updated_at) "
                            "VALUES(?,?,?)",
                            (str(name), json.dumps(route, sort_keys=True), time.time()))
                        n += 1
    except Exception:
        logger.exception("route-intent import failed (non-fatal)")
    return n


def _retire(path: Path) -> None:
    import contextlib
    bak = path.with_name(path.name + ".bak")
    with contextlib.suppress(OSError):
        if bak.exists():
            bak.unlink()
        path.rename(bak)


# ------------------------------------------------------------- housekeeping

LEGACY_BAK_GLOBS = ("state.json.bak", "imap_state.json.bak", "source-*.json.bak")


def _live_legacy_source_keys(conn: sqlite3.Connection) -> set:
    """The source-key set a live trigger can still own health under.

    Mirrors manage.status(): relay-backed triggers report under the plain
    'relay' key, everything else under '<source_type>:<name>'. A legacy
    source-<hash>.json / legacy:<hash> row whose hash matches none of
    these belongs to a deleted trigger — it can never be read again.
    """
    keys = set()
    for name, body in load_config_bodies(conn).items():
        src = body.get("source") if isinstance(body, dict) else None
        src = src if isinstance(src, dict) else {}
        if src.get("relay"):
            keys.add("relay")
        stype = src.get("type")
        if stype:
            keys.add(f"{stype}:{name}")
    return keys


def prune_legacy(home: Optional[Path] = None) -> dict:
    """Card C6 (audit F14): retire the import-once artifacts for good.

    C5 kept the migrated originals as *.bak "until the next release"; this
    IS that release, so on every process boot (once, after the authority is
    ready) we:

      * delete legacy *.bak files and the pre-C2 run_hidden.vbs launcher;
      * re-import any live legacy file NEWER than the db (a pre-0.2.5
        daemon kept writing state.json / imap_state.json / source-*.json
        until it was replaced; those late writes must not be lost silently)
        and delete the files;
      * drop source_health 'legacy:<hash>' rows whose hash matches no live
        trigger's source key (orphans by the trigger-name set).

    The db is the authority either way; nothing here can lose post-cutover
    state, and a box that already ran the migration with no stale files
    pays two globs and one query.
    """
    import contextlib
    import hashlib
    home = home or galvanize_home()
    gone: List[str] = []
    summary: Dict[str, Any] = {"files": gone}
    with write_tx() as conn:
        if not migrated(conn):
            return summary      # first-boot importer will handle the files
        keep_hashes = {hashlib.sha256(k.encode()).hexdigest()
                       for k in _live_legacy_source_keys(conn)}
        # 1. live legacy state files: newest-wins re-import, then delete.
        p = home / "state.json"
        if p.exists():
            with contextlib.suppress(Exception):
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _reimport_state_json(conn, data)
                    gone.append("state.json")
            _unlink(p)
        p = home / "imap_state.json"
        if p.exists():
            with contextlib.suppress(Exception):
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for name, val in data.items():
                        if not isinstance(val, dict):
                            continue
                        row = conn.execute(
                            "SELECT last_uid FROM imap_state WHERE name=?",
                            (str(name),)).fetchone()
                        try:
                            if row is None or int(val.get("last_uid", 0) or 0) > int(row[0]):
                                imap_put_sql(conn, str(name), val)
                                gone.append(f"imap_state.json[{name}]")
                        except (TypeError, ValueError):
                            pass
                    gone.append("imap_state.json")
                    _unlink(home / "imap_state.json")
        for fp in sorted(home.glob("source-*.json")):
            with contextlib.suppress(Exception):
                d = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    h = fp.stem[len("source-"):]
                    row = conn.execute(
                        "SELECT ts FROM source_health WHERE key=?",
                        ("legacy:" + h,)).fetchone()
                    if row is None or float(d.get("ts") or 0) >= float(row[0]):
                        conn.execute(
                            "INSERT OR REPLACE INTO source_health"
                            "(key, connected, error, ts, pid) VALUES(?,?,?,?,?)",
                            ("legacy:" + h, 1 if d.get("connected") else 0,
                             str(d.get("error") or "")[:500],
                             float(d.get("ts") or 0), int(d.get("pid") or 0)))
            gone.append(fp.name)
            _unlink(fp)
        # 2. *.bak retirement (C5: "kept until next release") + pre-C2 vbs.
        for pat in LEGACY_BAK_GLOBS:
            for fp in sorted(home.glob(pat)):
                gone.append(fp.name)
                _unlink(fp)
        vbs = home / "run_hidden.vbs"
        if vbs.exists():
            gone.append("run_hidden.vbs")
            _unlink(vbs)
        # 3. orphan legacy health rows (no file-backed match in live config).
        cur = conn.execute(
            "SELECT key FROM source_health WHERE key LIKE 'legacy:%'")
        for row in cur.fetchall():
            if row[0][len("legacy:"):] not in keep_hashes:
                conn.execute("DELETE FROM source_health WHERE key=?", (row[0],))
                gone.append(f"db:{row[0]}")
    return summary


def _unlink(path: Path) -> None:
    import contextlib
    with contextlib.suppress(OSError):
        path.unlink()


def _reimport_state_json(conn: sqlite3.Connection, data: dict) -> None:
    """Newest-wins replay of a stale pre-C5 state.json (see prune_legacy)."""
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        if key == "_daemon":
            try:
                row = conn.execute("SELECT ts FROM heartbeat WHERE id=1").fetchone()
                ts = float(val.get("heartbeat") or 0)
                if row is None or ts > float(row[0]):
                    conn.execute(
                        "INSERT OR REPLACE INTO heartbeat(id, pid, ts) VALUES(1,?,?)",
                        (int(val.get("pid") or 0), ts))
            except (TypeError, ValueError):
                pass
            continue
        try:
            last_fire = float(val["last_fire"]) if val.get("last_fire") else None
        except (TypeError, ValueError):
            last_fire = None
        row = conn.execute(
            "SELECT last_fire FROM trigger_state WHERE name=?", (key,)).fetchone()
        if row is None or (last_fire is not None
                           and float(row[0] or 0) < last_fire):
            try:
                fires_today = int(val.get("fires_today", 0) or 0)
            except (TypeError, ValueError):
                fires_today = 0
            conn.execute(
                "INSERT OR REPLACE INTO trigger_state"
                "(name, last_fire, last_error, last_error_at, fires_date, fires_today) "
                "VALUES(?,?,?,?,?,?)",
                (key, last_fire,
                 (str(val["last_error"])[:400] if val.get("last_error") else None),
                 float(val["last_error_at"]) if val.get("last_error_at") else None,
                 str(val.get("fires_date") or "") or None,
                 fires_today))


def migrate_from_files(home: Optional[Path] = None) -> dict:
    """Idempotent first-run importer: JSON state + YAML first-seed -> db.

    Old files are renamed *.bak (kept until next release). Runs inside ONE
    IMMEDIATE transaction: a half-imported authority cannot exist — either
    everything lands and migrated_at is set, or nothing does.
    """
    home = home or galvanize_home()
    summary: Dict[str, Any] = {"skipped": False}
    with write_tx() as conn:
        if migrated(conn):
            summary["skipped"] = True
            return summary
        _seed_config_from_yaml(conn, home)
        summary["state_entries"] = _import_state_json(conn, home)
        summary["imap_entries"] = _import_imap_json(conn, home)
        summary["health_entries"] = _import_source_health(conn, home)
        summary["route_intents"] = _import_route_intent(conn)
        _meta_set(conn, "migrated_at", time.time())
    for fname in ("state.json", "imap_state.json"):
        p = home / fname
        if p.exists():
            _retire(p)
    for p in sorted(home.glob("source-*.json")):
        _retire(p)
    logger.info("state migration: %s", summary)
    return summary


def ensure_ready() -> dict:
    """Open-the-db hook: migrate legacy files once per db path per process.

    Every public read/write entry calls this FIRST: a caller must never be
    able to observe (or render doctor output from) a half-booted authority.
    Re-entrant calls during an in-flight migration return immediately —
    the migration's own statements run on the open transaction.
    """
    key = str(db_path().resolve())
    if key in _ready_done or key in _ready_busy:
        return {"skipped": True}
    _ready_busy.add(key)
    try:
        summary = migrate_from_files()
        if not summary.get("error"):
            summary["pruned"] = prune_legacy()
    except sqlite3.Error as e:
        logger.error("store migration failed: %s", e)
        raise
    finally:
        _ready_busy.discard(key)
    _ready_done.add(key)
    return summary


def reset_process_cache() -> None:
    """Test hook: forget which db paths migrated (env changes GALVANIZE_HOME)."""
    _ready_done.clear()
    _ready_busy.clear()


# ------------------------------------------------------------------- doctor

def doctor_rows() -> List[dict]:
    """Doctor checks for the authority itself (offline, no daemon needed)."""
    rows: List[dict] = []
    path = db_path()
    try:
        def _q(conn):
            bad = conn.execute("PRAGMA quick_check").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            n = conn.execute("SELECT COUNT(*) FROM trigger_config").fetchone()[0]
            hist = conn.execute("SELECT COUNT(*) FROM fire_history").fetchone()[0]
            return bad, journal, n, hist
        bad, journal, n_triggers, n_hist = read(_q)
    except sqlite3.Error as e:
        return [{"name": "state store", "ok": False,
                 "note": f"galvanize.db unreadable: {type(e).__name__}: {e}"}]
    if bad != "ok":
        rows.append({"name": "state store", "ok": False, "note": f"quick_check: {bad}"})
    else:
        note = (f"galvanize.db quick_check ok ({journal}, {n_triggers} trigger rows, "
                f"{n_hist} fire-history rows)")
        rows.append({"name": "state store", "ok": True, "note": note})

    state, note, rev = config_state()
    if state == "ok":
        rows.append({"name": "config authority", "ok": True,
                     "note": f"SQLite revision {rev} is the authority; "
                             "triggers.yaml is import/export only"})
    elif state == "corrupt":
        rows.append({"name": "config authority", "ok": False,
                     "note": f"UNHEALTHY: last import was invalid — kept committed "
                             f"last-known-good (rev {rev}); {note}"})
    else:
        rows.append({"name": "config authority", "ok": False,
                     "note": "UNHEALTHY: no valid config has ever been activated "
                             "(missing: no triggers to seed from) — this is NOT "
                             "'no triggers'; import: galvanize config import <path>"})
    return rows


def summary() -> dict:
    state, note, rev = config_state()
    return {"revision": rev, "state": state, "note": note, "db": str(db_path())}
