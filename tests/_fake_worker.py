"""Fake supervisor worker for C2 tests: a minimal process that speaks the
READY protocol against GALVANIZE_HOME instead of the real daemon.

Modes (argv[1]):
  wedged       start, never post READY (simulates the pre-main wedge, F1's
               61864: alive, silent, contributing nothing)
  bogus_token  posts READY with the wrong token (unauthenticated)
  ok_exit      READY + heartbeat, live ~1.5s, exit 0
  ticker       READY + heartbeat forever (kill externally to test respawn)
  one_beat     READY + ONE heartbeat, then hang (tests the liveness contract)
  permfail     READY carrying a PERMANENT capability failure, then exit
"""
import json
import os
import sys
import time
from pathlib import Path

mode = sys.argv[1]
home = Path(os.environ["GALVANIZE_HOME"])
token = os.environ.get("GALVANIZE_READY_TOKEN", "")
pid = os.getpid()

# count spawns: one line per worker that got far enough to touch base
with open(home / "borns.log", "a", encoding="utf-8") as fh:
    fh.write(f"{pid} {mode}\n")


def post(permanent=False):
    watchers = ({"mbox": {"type": "imap", "state": "failed-deps",
                          "permanent": True, "error": "imap-tools missing"}}
                if permanent else
                {"fake": {"type": "folder", "state": "running",
                          "permanent": False}})
    payload = {
        "token": "WRONG-TOKEN" if mode == "bogus_token" else token,
        "pid": pid,
        "exe": sys.executable,
        "version": "test",
        "python": sys.version.split()[0],
        "ts": time.time(),
        "capabilities": {"imap": not permanent, "folder": True,
                         "serve": False, "relay": False},
        "watchers": watchers,
    }
    tmp = home / ".ready.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, home / "ready.json")


def beat():
    # C5: heartbeat is a galvanize.db row (same contract the real worker uses).
    sys.path.insert(0, os.getcwd())
    from galvanize import store
    store.set_heartbeat(pid=pid)


if mode == "wedged":
    time.sleep(60)
elif mode == "bogus_token":
    post()
    time.sleep(60)
elif mode == "ok_exit":
    post()
    beat()
    time.sleep(1.5)
elif mode == "ticker":
    post()
    while True:
        beat()
        time.sleep(0.2)
elif mode == "one_beat":
    post()
    beat()
    time.sleep(60)
elif mode == "permfail":
    post(permanent=True)
    beat()
    time.sleep(1.5)
else:
    sys.exit(9)
