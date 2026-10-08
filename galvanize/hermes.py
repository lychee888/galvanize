"""Hermes webhook-lane integration.

Everything needed to register a route and wake a Hermes session:
  - read the gateway webhook port from the user's config.yaml (never hardcoded)
  - manage our routes inside ~/.hermes/webhook_subscriptions.json
    (hot-reloaded by the gateway — no restart needed), tagged with
    managed_by: galvanize so removal never touches user-created routes
  - POST events with generic HMAC V2 signatures (timestamp-bound, replay-safe)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from .events import Event
from .paths import hermes_home

DEFAULT_PORT = 8644
_SUBS_FILENAME = "webhook_subscriptions.json"
MANAGED_BY = "galvanize"


class HermesNotConfigured(Exception):
    """Webhook platform is not enabled in the user's Hermes config."""


def _read_hermes_config() -> dict:
    p = hermes_home() / "config.yaml"
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def webhook_enabled(cfg: Optional[dict] = None) -> bool:
    cfg = cfg if cfg is not None else _read_hermes_config()
    wh = (cfg.get("platforms") or {}).get("webhook") or {}
    return bool(wh.get("enabled"))


def webhook_port(cfg: Optional[dict] = None) -> int:
    cfg = cfg if cfg is not None else _read_hermes_config()
    wh = (cfg.get("platforms") or {}).get("webhook") or {}
    try:
        return int((wh.get("extra") or {}).get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def webhook_base_url(cfg: Optional[dict] = None) -> str:
    cfg = cfg if cfg is not None else _read_hermes_config()
    wh = (cfg.get("platforms") or {}).get("webhook") or {}
    host = (wh.get("extra") or {}).get("host")
    display = "localhost" if not host or host in {"0.0.0.0", "::"} else str(host)
    if ":" in display and not display.startswith("["):
        display = f"[{display}]"
    return f"http://{display}:{webhook_port(cfg)}"


def health_url(cfg: Optional[dict] = None) -> str:
    return webhook_base_url(cfg) + "/health"


def _subs_path() -> Path:
    return hermes_home() / _SUBS_FILENAME


def _load_subs() -> Dict[str, dict]:
    p = _subs_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_subs(subs: Dict[str, dict]) -> None:
    """Write the subscriptions file atomically, preserving foreign routes.

    The gateway hot-reloads this file on request (mtime-gated), so a saved
    route is live within one event — no gateway restart.
    """
    path = _subs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(subs, fh, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register_route(
    trigger_name: str,
    secret: str,
    *,
    deliver: str = "log",
    description: str = "",
    events: Optional[list] = None,
    prompt: str = "{prompt}",
) -> str:
    """Create/refresh our managed route. Returns the webhook URL.

    Default route prompt is "{prompt}" — galvanize renders the real prompt
    server-side and passes it inside the payload, so one rendering path
    serves every wake kind. webhook-kind triggers pass the user's own
    template instead: those events arrive straight from the external
    service, and the Hermes lane renders the template itself.
    """
    subs = _load_subs()
    existing = subs.get(trigger_name)
    if existing is not None and existing.get("managed_by") not in (None, MANAGED_BY):
        raise ValueError(
            f"route name '{trigger_name}' already exists as a user-created "
            "webhook subscription; rename the trigger to avoid clobbering it"
        )
    # Hermes lanes parse deliver as a bare platform NAME; the chat rides in
    # deliver_extra.chat_id. "telegram:12345" would fail the Platform() lookup
    # at send time ("Unknown deliver type") and every fire dies post-response.
    deliver_type, _, deliver_chat = deliver.partition(":")
    route: Dict[str, Any] = {
        "description": description or f"galvanize trigger: {trigger_name}",
        # None -> default; [] -> explicitly ALLOW ALL (external services
        # bring their own event names). `events or [...]` would coerce the
        # empty list and silently filter every real webhook.
        "events": list(events) if events is not None else ["galvanize"],
        "secret": secret,
        "prompt": prompt or "{prompt}",
        "deliver": deliver_type or "log",
        "managed_by": MANAGED_BY,
    }
    if deliver_chat:
        route["deliver_extra"] = {"chat_id": deliver_chat}
    if existing and existing.get("secret"):
        route["secret"] = existing["secret"]  # keep stable secret across re-adds
    elif existing is None and not secret:
        raise ValueError("new route needs a secret")
    subs[trigger_name] = route
    _save_subs(subs)
    return f"{webhook_base_url()}/webhooks/{trigger_name}"


def route_secret(trigger_name: str) -> Optional[str]:
    route = _load_subs().get(trigger_name)
    if route and route.get("managed_by") == MANAGED_BY:
        return route.get("secret") or None
    return None


def unregister_route(trigger_name: str) -> bool:
    subs = _load_subs()
    route = subs.get(trigger_name)
    if route is None:
        return True
    if route.get("managed_by") != MANAGED_BY:
        return False  # never delete routes we didn't create
    del subs[trigger_name]
    _save_subs(subs)
    return True


def gateway_running(cfg: Optional[dict] = None, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(health_url(cfg), timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def sign_v2(secret: str, body: bytes, timestamp: int) -> str:
    """Generic HMAC V2: hex HMAC-SHA256 of "<timestamp>.<body>"."""
    return hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()


def post_event(
    trigger_name: str,
    event: Event,
    *,
    prompt: str,
    secret: Optional[str] = None,
    timeout: float = 10.0,
    base_url: Optional[str] = None,
) -> Tuple[int, dict]:
    """POST a rendered event into the Hermes webhook lane.

    Returns (status_code, response_json). 202 = a fresh Hermes session was
    spawned; the lane handles templating, delivery, and session close.
    Raises HermesNotConfigured if the webhook platform is disabled.
    """
    cfg = _read_hermes_config()
    if not webhook_enabled(cfg):
        raise HermesNotConfigured(
            "Hermes webhook platform is not enabled — run 'galvanize init' "
            "or enable platforms.webhook in config.yaml"
        )
    secret = secret or route_secret(trigger_name)
    if not secret:
        raise HermesNotConfigured(
            f"no galvanize-managed route '{trigger_name}' — re-add the trigger"
        )

    body_obj = event.to_body()
    body_obj["prompt"] = prompt
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    ts = int(time.time())
    url = (base_url or webhook_base_url(cfg)) + f"/webhooks/{trigger_name}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": str(ts),
            "X-Webhook-Signature-V2": sign_v2(secret, body, ts),
            # Fresh delivery id: the lane's 1h idempotency cache must not
            # swallow a legitimate re-fire.
            "X-Request-ID": uuid.uuid4().hex,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            err = {"error": str(e)}
        return e.code, err
