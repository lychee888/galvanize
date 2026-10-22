"""The galvanize daemon: `galvanize run`.

Runs the folder watchers, polls triggers.yaml for changes (mtime, 2s),
rebuilds watchers on change, and writes a heartbeat to state.json so
`galvanize status` can tell "quiet" apart from "dead".

v0.1 sources needing the daemon: folder. `emit` runs inline in the CLI,
and webhook-type triggers are received by the Hermes gateway itself —
that is the point of the architecture.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Dict

from . import hermes as hermes_mod
from . import state
from . import store
from .bus import TriggerBus
from .config import Trigger, load_triggers
from .events import Event
from .paths import ensure_home, log_path
from .sources.folder import FolderWatcher

logger = logging.getLogger("galvanize.daemon")


def _setup_logging(verbose: bool) -> None:
    ensure_home()
    # Rotating (audit F10, folded into C2): the old plain FileHandler grew
    # ~13 MB/yr unbounded. Same budget as supervisor.log: 2 MB x 3 backups.
    handler = RotatingFileHandler(str(log_path()), maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("galvanize")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    if verbose:
        root.addHandler(logging.StreamHandler())


class Daemon:
    def __init__(self) -> None:
        self.bus = TriggerBus()
        self._stop = threading.Event()
        self._watcher: FolderWatcher | None = None
        self._folder_triggers: Dict[str, dict] = {}
        self._imap_watchers: Dict[str, dict] = {}  # name -> (watcher, sig)
        self._imap_deps_missing: list = []
        self._claim = None

    # ---- watcher lifecycle -------------------------------------------------

    def _folder_signature(self, triggers: Dict[str, Trigger]) -> dict:
        sig = {}
        for name, t in triggers.items():
            if t.enabled and t.source_type == "folder":
                sig[name] = t.to_dict()
        return sig

    def _imap_signature(self, triggers: Dict[str, Trigger]) -> dict:
        sig = {}
        for name, t in triggers.items():
            if t.enabled and t.source_type == "imap":
                sig[name] = t.to_dict()
        return sig

    def _rebuild_imap_watchers(self, triggers: Dict[str, Trigger]) -> None:
        try:
            from .sources.imap import ImapWatcher
        except ImportError:
            # e.g. a git checkout without `pip install -e .[imap]`-style deps:
            # one loud error beats killing the whole daemon at startup.
            # C2 READY: this class of failure can NOT be fixed by respawn —
            # record it permanent so the supervisor can halt instead of
            # looping (judge: capability-permanent failure => stop retrying).
            missing = sorted(self._imap_signature(triggers))
            self._imap_deps_missing = missing
            if missing:
                logger.error("imap trigger(s) configured but 'imap-tools' is "
                             "not importable — daemon continues without imap "
                             "watching (pip install imap-tools): %s",
                             ", ".join(missing))
            return
        self._imap_deps_missing = []
        from . import secrets as secrets_mod

        sig = self._imap_signature(triggers)
        # stop watchers that vanished or changed
        for name in list(self._imap_watchers):
            w, old_sig = self._imap_watchers[name]
            if sig.get(name) != old_sig:
                w.stop()
                del self._imap_watchers[name]
        # start new ones
        for name, td in sig.items():
            if name in self._imap_watchers:
                continue
            t = triggers[name]
            secret_key = str(t.source.get("secret_key", "")) or f"imap:{t.source.get('user', name)}"
            watcher = ImapWatcher(
                t, self._make_emit(name),
                password_provider=(lambda k: (lambda: secrets_mod.get_secret(k)))(secret_key),
                secret_key=secret_key,
            )
            watcher.start()
            self._imap_watchers[name] = (watcher, td)
            logger.info("imap watcher started: %s (%s@%s/%s)", name,
                        t.source.get("user"), t.source.get("host"),
                        t.source.get("folder", "INBOX"))

    def _rebuild_watchers(self, triggers: Dict[str, Trigger]) -> None:
        sig = self._folder_signature(triggers)
        if self._watcher is None or sig != self._folder_triggers:
            if self._watcher is not None:
                logger.info("triggers changed — rebuilding folder watchers")
                self._watcher.stop()
                self._watcher = None
            watcher = FolderWatcher()
            for name, td in sig.items():
                t = triggers[name]
                watcher.add(t, self._make_emit(name))
            watcher.start()
            self._watcher = watcher
            self._folder_triggers = sig
            for name in sig:
                state.set_source_health('folder:' + name, True)
            logger.info("watching %d folder trigger(s): %s", len(sig), ", ".join(sig) or "(none)")
        self._rebuild_imap_watchers(triggers)
        self._rebuild_relay(trigger_has_relay=any(
            t.enabled and t.source.get("relay") for t in triggers.values()))

    def _rebuild_relay(self, trigger_has_relay: bool) -> None:
        from .config import GlobalConfig
        from .sources.relay import RelayWatcher
        from . import secrets as secrets_mod

        url = GlobalConfig.load().relay_url
        want = bool(trigger_has_relay and url)
        have = getattr(self, "_relay", None)
        if want and (have is None or have.url != url):
            if have is not None:
                have.stop()
            token = secrets_mod.get_secret("relay:token") or ""

            def _emit_by_route(route: str, evt):
                ok, detail = self.bus.handle_named(route, evt)
                if not ok:
                    logger.error("relay route %s: %s", route, detail)
                return ok, detail

            self._relay = RelayWatcher(url, token, _emit_by_route)
            self._relay.start()
            logger.info("relay poller started: %s", url)
        elif not want and have is not None:
            have.stop()
            self._relay = None
            logger.info("relay poller stopped")

    def _make_emit(self, trigger_name: str):
        def _emit(evt: Event) -> None:
            # dedupe per-event on the payload path so a file re-added after
            # removal doesn't get swallowed by a stale key
            ok, detail = self.bus.handle_named(trigger_name, evt)
            if not ok:
                logger.error("trigger %s: %s", trigger_name, detail)
        return _emit

    # ---- READY protocol (card C2 §2) -----------------------------------------

    def _readiness(self, serve_ok: bool = False) -> dict:
        """Payload for supervisor.post_worker_ready: interpreter identity,
        version, capabilities and per-watcher status. A watcher whose hard
        dependency cannot be imported is a PERMANENT failure (respawning
        cannot fix it; the supervisor halts rather than looping); anything
        network/auth shaped stays transient."""
        import platform
        from . import __version__
        watchers: Dict[str, dict] = {}
        for name in self._folder_triggers:
            alive = bool(self._watcher and self._watcher._observer.is_alive())
            watchers[name] = {"type": "folder",
                              "state": "running" if alive else "dead",
                              "permanent": False}  # respawn restarts observers
        for name, (w, _sig) in self._imap_watchers.items():
            thread_alive = bool(getattr(w, "_thread", None)
                                and w._thread.is_alive())
            watchers[name] = {
                "type": "imap",
                "state": ("connected" if getattr(w, "connected", False)
                          else "backoff" if thread_alive else "dead"),
                "permanent": False,  # auth/network errors retry forever; only
                                     # dep-missing below is permanent
            }
        for name in self._imap_deps_missing:
            watchers[name] = {"type": "imap", "state": "failed-deps",
                              "permanent": True,
                              "error": "imap-tools not importable"}
        imap_ok = not self._imap_deps_missing
        # C4: ride the SAME capability manifest the installer probed
        # (galvanize.capability) so doctor's three-way diff compares like
        # with like; runtime-observed imap/serve/relay flags win.
        from . import capability as _cap
        cm = _cap.collect()
        caps = dict(cm.get("capabilities") or {})
        caps.update({"imap": imap_ok, "serve": bool(serve_ok),
                     "relay": getattr(self, "_relay", None) is not None})
        return {
            "pid": os.getpid(),
            "exe": sys.executable,
            "version": __version__,
            "python": platform.python_version(),
            "caps_schema": cm.get("schema"),
            "cap_failures": cm.get("failures") or [],
            "capabilities": caps,
            "watchers": watchers,
        }

    # ---- main loop ---------------------------------------------------------

    def run(self, verbose: bool = False) -> None:
        _setup_logging(verbose)
        ensure_home()
        # Single instance, by CONSTRUCTION (audit F1 / card C2): the named
        # mutex is the authority, not a startup-only heartbeat peek (that
        # was the TOCTOU that let two daemons coexist for 20h). Under a
        # supervisor the claim was already made before this process existed;
        # a direct `galvanize run` must claim the same authority itself so
        # it can never join (or race) a running supervisor-managed daemon.
        supervised = bool(os.environ.get("GALVANIZE_READY_TOKEN"))
        self._claim = None
        if not supervised:
            from . import supervisor as sup
            try:
                self._claim = sup.claim_mutex(
                    os.environ.get("GALVANIZE_PROFILE", "").strip() or "default")
            except sup.MutexHeld as held:
                snap = sup.snapshot() or {}
                why = (f"supervisor pid {snap.get('supervisor_pid')} owns it"
                       if snap else f"authority held ({held.name})")
                logger.error("daemon: refusing to start; %s", why)
                print(f"  ✖ galvanize daemon already running — {why};"
                      f" stop it first: galvanize daemon stop")
                return
        # Belt-and-braces refusal: a fresh heartbeat from a process that is
        # ACTUALLY ALIVE means something else owns the watchers (a legacy
        # pre-C2 daemon, or an unmanaged run that raced us). A fresh-looking
        # beat from a recently-KILLED worker must not stall the supervisor's
        # respawn for 60s (drill 2026-10-21 21:22): pid-liveness, not age,
        # is the fact; age is only the tiebreaker for a live process we
        # cannot probe.
        live = state.live_daemon_pid()
        if live and live != os.getpid():
            from . import supervisor as _sup
            if _sup.pid_alive(live):
                if self._claim is not None:
                    self._claim.release()
                    self._claim = None
                logger.error("daemon: another instance is alive (pid %d, heartbeat fresh) "
                             "— refusing to start a second; stop it first: galvanize daemon stop",
                             live)
                print(f"  ✖ galvanize daemon already running (pid {live}).")
                return
            logger.warning("daemon: adopting stale heartbeat from dead pid %d", live)
        # serve = versioned external API (dsh plugin, mcp clients). Embedded
        # on a daemon thread so at-login autostart keeps it alive with zero
        # extra services; harmless if nothing connects.
        from . import serve as _serve
        serve_httpd = None
        try:
            serve_httpd = _serve.run(port=0, block=False)
            logger.info("serve api up on port %s (api v%s)",
                        serve_httpd.server_address[1], _serve.API_VERSION)
        except Exception:
            logger.exception("serve api failed to start (continuing without it)")
        try:
            store.ensure_ready()   # C5: import legacy JSON/YAML state once
            triggers = load_triggers()
            self._rebuild_watchers(triggers)
            last_rev = store.current_revision()
            logger.info("daemon started (%d triggers, config rev %d)",
                        len(triggers), last_rev)
            state.set_heartbeat()
            last_beat = time.time()
            if supervised:
                # Card C2 §2: watchers are built and the first beat exists —
                # post the authenticated READY the supervisor waits on.
                from . import supervisor as sup
                sup.post_worker_ready(self._readiness(serve_ok=bool(serve_httpd)))

            def _term(signum, frame):
                self._stop.set()

            last_recon = 0.0

            for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
                if sig is not None:
                    try:
                        signal.signal(sig, _term)
                    except ValueError:
                        pass  # non-main thread

            while not self._stop.is_set():
                time.sleep(2.0)
                # C5: hot-reload keys off the DB revision, not file mtime —
                # every editor (CLI, plugin, serve) bumps the revision in
                # the same transaction that lands the rows.
                try:
                    rev = store.current_revision()
                except Exception:
                    logger.exception("config revision poll failed — retry next tick")
                    continue
                if rev != last_rev:
                    last_rev = rev
                    try:
                        self._rebuild_watchers(load_triggers())
                    except Exception:
                        logger.exception("watcher rebuild failed — retrying next poll")
                if time.time() - last_beat >= 15:
                    state.set_heartbeat()
                    for name in self._folder_triggers:
                        state.set_source_health('folder:' + name, bool(self._watcher and self._watcher._observer.is_alive()))
                    last_beat = time.time()
                    if time.time() - last_recon >= 60:
                        # C5: the gateway file is shared-writer territory;
                        # converge managed routes from intent every minute so
                        # a gateway/user clobber is self-healing, not silent.
                        last_recon = time.time()
                        try:
                            rec = hermes_mod.reconcile_routes()
                            if rec["repaired"] or rec["adopted"]:
                                logger.info("route reconcile: repaired=%s adopted=%s conflicts=%s",
                                            rec["drifted"], rec["adopted"], rec["conflicts"])
                        except Exception:
                            logger.exception("route reconcile failed (retry next beat)")
        finally:
            relay = getattr(self, '_relay', None)
            if relay is not None:
                relay.stop()
            if self._watcher:
                self._watcher.stop()
            for w, _ in self._imap_watchers.values():
                try:
                    w.stop()
                except Exception:
                    pass
            if serve_httpd is not None:
                try:
                    serve_httpd.shutdown()
                    from .serve import serve_info_path
                    serve_info_path().unlink(missing_ok=True)
                except Exception:
                    pass
            state.clear_heartbeat()
            if getattr(self, "_claim", None) is not None:
                self._claim.release()
                self._claim = None
            logger.info("daemon stopped")
