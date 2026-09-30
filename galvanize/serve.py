"""galvanize serve — versioned local API for external consumers.

The contract (fixed with galvanize-dsh PLAN rev.1 §3; bump api_version on
breaking changes, never silently):
  loopback-only HTTP; discovery via ~/.galvanize/serve.json {port,pid,ts};
  bearer token ~/.galvanize/serve.token (0600, auto-created).
  GET  /version           -> {core_version, api_version}
  POST /manage/<op>       -> op in add|remove|test|status|emit|list
                             JSON body = op kwargs; returns op result JSON.

The Hermes *dashboard* does NOT use this (in-process plugin routes are
simpler); this door exists for out-of-process consumers: the DSH plugin,
`galvanize mcp` (which also runs embedded), future tooling.

Stdlib http.server ThreadingHTTPServer — deliberately dependency-free.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__
from .paths import ensure_home, galvanize_home

API_VERSION = 1


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def serve_info_path() -> Path:
    return galvanize_home() / "serve.json"


def serve_token_path() -> Path:
    return galvanize_home() / "serve.token"


def surfaces_path() -> Path:
    return galvanize_home() / "surfaces.json"


def ensure_token() -> str:
    p = serve_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except OSError:
            pass
    tok = _secrets.token_urlsafe(32)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(tok)
    return tok


def read_token() -> Optional[str]:
    try:
        tok = serve_token_path().read_text(encoding="utf-8").strip()
        return tok or None
    except OSError:
        return None


def server_info() -> Optional[dict]:
    try:
        info = json.loads(serve_info_path().read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else None
    except Exception:
        return None


def write_surfaces(mapping: Dict[str, str]) -> None:
    ensure_home()
    p = surfaces_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def read_surfaces() -> Dict[str, str]:
    try:
        d = json.loads(surfaces_path().read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


# ------------------------------------------------------------------ dispatch

def call_op(op: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one manage op. Raises ApiError on bad requests."""
    from . import manage

    if op == "version":
        return {"core_version": __version__, "api_version": API_VERSION}
    if op == "list":
        from .config import load_triggers
        ts = load_triggers()
        return {"ok": True, "triggers": {n: t.to_dict() for n, t in ts.items()}}
    if op == "status":
        return manage.status()
    if op == 'configure_dsh_profile':
        import re
        from .config import GlobalConfig
        profile = str(body.get('wake_profile', ''))
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', profile):
            raise ApiError(400, 'invalid DSH wake profile')
        gc = GlobalConfig.load()
        gc.dsh_wake_profile = profile
        gc.save()
        return {'ok': True, 'wake_profile': profile}
    if op == 'retry_relay':
        return manage.retry_relay_event(str(body.get('id', '')))
    if op == "add":
        kw = dict(body)
        kind = kw.pop("kind", "")
        target = kw.pop("target", "")
        return manage.add_trigger(kind, target, **kw)
    if op == "remove":
        return manage.remove_trigger_op(str(body.get("name", "")))
    if op == "set_enabled":
        from .config import set_enabled as _set_enabled
        ok = _set_enabled(str(body.get("name", "")), bool(body.get("enabled", True)))
        return {"ok": ok, **({} if ok else {"error": "no such trigger"})}
    if op == "test":
        return manage.test_trigger(str(body.get("name", "")), body.get("payload"))
    if op == "emit":
        return manage.emit(str(body.get("name", "")), body.get("payload"))
    raise ApiError(404, f"unknown op '{op}'")


class _Handler(BaseHTTPRequestHandler):
    token: str = ""

    def log_message(self, fmt, *args):  # silence stderr spam
        pass

    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {self.token}"

    def do_GET(self):
        if self.path == "/version":
            # version handshake stays token-free: the DSH installer must be
            # able to probe "is a compatible core here?" before any secret.
            self._send(200, {"core_version": __version__, "api_version": API_VERSION})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        if not self.path.startswith("/manage/"):
            self._send(404, {"error": "not found"})
            return
        op = self.path[len("/manage/"):].strip("/")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            self._send(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._send(400, {"error": f"bad JSON: {e}"})
            return
        try:
            result = call_op(op, body)
            self._send(200, result)
        except ApiError as e:
            self._send(e.code, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def run(port: int = 0, block: bool = True) -> Optional[ThreadingHTTPServer]:
    """Start serve. port=0 -> OS-assigned. Writes serve.json while alive.

    Refuses to start a second serve when a live one is already discoverable
    (a double-started daemon used to clobber serve.json, stranding the
    first daemon's consumers). Callers catch and continue without it.
    """
    ensure_home()
    existing = running_info()
    if existing and int(existing.get("pid", -1)) != os.getpid():
        raise RuntimeError(
            f"galvanize serve already live on port {existing['port']} "
            f"(pid {existing.get('pid')}) — not starting a second one")
    token = ensure_token()
    _Handler.token = token
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    actual = httpd.server_address[1]
    serve_info_path().write_text(json.dumps(
        {"port": actual, "pid": os.getpid(), "ts": time.time(), "api_version": API_VERSION}
    ), encoding="utf-8")
    if not block:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd
    try:
        httpd.serve_forever()
    finally:
        try:
            serve_info_path().unlink(missing_ok=True)
        except OSError:
            pass


def running_info(probe: bool = True) -> Optional[dict]:
    """Live serve info, verified with a /version GET (never os.kill on
    Windows — sig 0 there means TerminateProcess, not a probe)."""
    info = server_info()
    if not info:
        return None
    if not probe:
        return info
    try:
        import urllib.request
        url = f"http://127.0.0.1:{int(info['port'])}/version"
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("api_version") == API_VERSION:
            return info
    except Exception:
        pass
    return None
