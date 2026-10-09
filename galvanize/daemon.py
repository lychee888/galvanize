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
import threading
import time
from typing import Dict

from . import state
from .bus import TriggerBus
from .config import Trigger, load_triggers, triggers_path_mtime
from .events import Event
from .paths import ensure_home, log_path, pid_path
from .sources.folder import FolderWatcher

logger = logging.getLogger("galvanize.daemon")


def _setup_logging(verbose: bool) -> None:
    ensure_home()
    handler = logging.FileHandler(log_path(), encoding="utf-8")
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
            if self._imap_signature(triggers):
                logger.error("imap trigger(s) configured but 'imap-tools' is "
                             "not importable — daemon continues without imap "
                             "watching (pip install imap-tools)")
            return
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

    # ---- main loop ---------------------------------------------------------

    def run(self, verbose: bool = False) -> None:
        _setup_logging(verbose)
        ensure_home()
        # Single instance: a second daemon double-watches every mailbox (both
        # connect, both race to advance the UID anchor, one may miss events)
        # and desyncs heartbeat pid from watcher health pids, which makes
        # `status` lie about staleness. Refuse to join an alive daemon.
        live = state.live_daemon_pid()
        if live and live != os.getpid():
            logger.error("daemon: another instance is alive (pid %d, heartbeat fresh) "
                         "— refusing to start a second; stop it first: galvanize daemon stop",
                         live)
            print(f"  ✖ galvanize daemon already running (pid {live}).")
            return
        pid_path().write_text(str(__import__("os").getpid()), encoding="utf-8")
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
            triggers = load_triggers()
            self._rebuild_watchers(triggers)
            last_mtime = triggers_path_mtime()
            logger.info("daemon started (%d triggers)", len(triggers))
            state.set_heartbeat()
            last_beat = time.time()

            def _term(signum, frame):
                self._stop.set()

            for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
                if sig is not None:
                    try:
                        signal.signal(sig, _term)
                    except ValueError:
                        pass  # non-main thread

            while not self._stop.is_set():
                time.sleep(2.0)
                mtime = triggers_path_mtime()
                if mtime != last_mtime:
                    last_mtime = mtime
                    try:
                        self._rebuild_watchers(load_triggers())
                    except Exception:
                        logger.exception("watcher rebuild failed — retrying next poll")
                if time.time() - last_beat >= 15:
                    state.set_heartbeat()
                    for name in self._folder_triggers:
                        state.set_source_health('folder:' + name, bool(self._watcher and self._watcher._observer.is_alive()))
                    last_beat = time.time()
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
            try:
                pid_path().unlink(missing_ok=True)
            except OSError:
                pass
            logger.info("daemon stopped")
