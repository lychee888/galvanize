"""Hermes webhook-lane integration.

Everything needed to register a route and wake a Hermes session:
  - read the gateway webhook port from the user's config.yaml (never hardcoded)
  - manage our routes inside ~/.hermes/webhook_subscriptions.json
    (hot-reloaded by the gateway — no restart needed), tagged with
    managed_by: galvanize so removal never touches user-created routes
  - POST events with generic HMAC V2 signatures (timestamp-bound, replay-safe)

C5 shared-writer note (judge verdict): webhook_subscriptions.json stays
GATEWAY-OWNED — its format is the gateway's contract and the gateway can
rewrite it at any time. Galvanize never edits it blindly: every managed
route mutation runs as one short BEGIN IMMEDIATE transaction on galvanize.db
(no network inside), so concurrent galvanize processes serialize their
file writes, and the desired route is ALSO recorded as an intent row.
A reconciler (daemon periodic + `doctor --fix`) converges the file back to
intent. Residual risk, documented and accepted: a gateway write landing
between our transaction commit and the gateway's next read can still
transiently revert a route — reconciliation is what bounds that window,
optimistic locking on someone else's file is not.
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
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .events import Event
from .paths import hermes_home

DEFAULT_PORT = 8644
_SUBS_FILENAME = "webhook_subscriptions.json"
MANAGED_BY = "galvanize"

# Wake-session tool grants. The gateway restricts webhook lanes to the
# `hermes-webhook` composite (web_search, web_extract, vision_analyze,
# clarify) unless a route carries a "toolsets" key (gateway/platforms/
# webhook.py toolsets_for_source — consumed gateway/run_turn.py
# _resolve_enabled_toolsets_for_source). PARITY_TOOLSETS is the gateway's
# `default` distribution ("all available tools, all the time",
# toolset_distributions.py; every member sits at 100%, so it resolves to
# exactly these names) — i.e. what a cron/main Hermes session gets. The
# gateway re-validates each name through resolve_toolset at spawn time and
# drops unknown ones, so an outdated entry here degrades to a no-op, never
# a hard failure.
PARITY_TOOLSETS: Tuple[str, ...] = ("browser", "file", "image_gen",
                                    "terminal", "vision", "web")


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


class _KeepSentinel:
    """mutate() return value: abort everything, change neither file nor rows."""


_KEEP = _KeepSentinel()


def _serialized_route_write(mutate):
    """One BEGIN IMMEDIATE txn around: read gateway file -> mutate -> write
    file -> land the intent row. `mutate(subs, conn)` returns either
    (name, route) to record as intent, None (file written, no intent up),
    or _KEEP (change nothing); raising aborts with NO file write and NO
    row change.

    The file write happens while we hold the db write lock: two galvanize
    processes (CLI + daemon serve + dashboard plugin) can never interleave
    load-modify-save and lose each other's route. No network inside —
    webhook_base_url()/config reads run before or after, never within.
    """
    from . import store
    with store.write_tx() as conn:
        subs = _load_subs()
        intent = mutate(subs, conn)
        if intent is _KEEP:
            conn.rollback()
            return
        _save_subs(subs)
        if intent is not None:
            name, route = intent
            store._intent_upsert(conn, name, route)


def register_route(
    trigger_name: str,
    secret: str,
    *,
    deliver: str = "log",
    description: str = "",
    events: Optional[list] = None,
    prompt: str = "{prompt}",
    toolsets: Optional[list] = None,
) -> str:
    """Create/refresh our managed route. Returns the webhook URL.

    Default route prompt is "{prompt}" — galvanize renders the real prompt
    server-side and passes it inside the payload, so one rendering path
    serves every wake kind. webhook-kind triggers pass the user's own
    template instead: those events arrive straight from the external
    service, and the Hermes lane renders the template itself.

    `toolsets` (v0.2.4) decides the wake session's tool access, written as
    the route's per-route "toolsets" key the gateway consumes at spawn:
      None  -> keep an existing non-empty override, else PARITY_TOOLSETS
      []    -> explicit restricted (alert-only): no key written
      [...] -> grant exactly those toolset names (gateway drops unknowns)
    """
    def _mutate(subs: Dict[str, dict], conn) -> Optional[tuple]:
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
        if toolsets is not None:
            grant = [str(t).strip() for t in toolsets if str(t).strip()]
        else:
            prev = existing.get("toolsets") if isinstance(existing, dict) else None
            grant = ([str(t).strip() for t in prev if str(t).strip()]
                     if isinstance(prev, list) and prev
                     else list(PARITY_TOOLSETS))
        if grant:  # [] (explicit restricted) writes no key -> gateway default
            route["toolsets"] = grant
        if existing and existing.get("secret"):
            route["secret"] = existing["secret"]  # keep stable secret across re-adds
        elif existing is None and not secret:
            raise ValueError("new route needs a secret")
        subs[trigger_name] = route
        return (trigger_name, route)

    _serialized_route_write(_mutate)
    return f"{webhook_base_url()}/webhooks/{trigger_name}"


def route_secret(trigger_name: str) -> Optional[str]:
    route = _load_subs().get(trigger_name)
    if route and route.get("managed_by") == MANAGED_BY:
        return route.get("secret") or None
    return None


def route_toolsets(trigger_name: str) -> Optional[list]:
    """The toolsets override granted to OUR route, None when the key is
    absent (restricted default — explicit or pre-v0.2.4 legacy) or the
    route is not galvanize-managed."""
    route = _load_subs().get(trigger_name)
    if route and route.get("managed_by") == MANAGED_BY:
        ts = route.get("toolsets")
        if isinstance(ts, list):
            cleaned = [str(t).strip() for t in ts if str(t).strip()]
            return cleaned or None
    return None


def set_route_toolsets(trigger_name: str, grant: Optional[list]) -> bool:
    """Replace (empty grant -> drop) the toolsets key on a managed route,
    touching no other field. Migration-safe: never rebuilds a route dict.
    Serialized through the db + intent rows like every other route write."""
    from . import store
    hit = {"found": False}

    def _mutate(subs: Dict[str, dict], conn) -> Optional[tuple]:
        route = subs.get(trigger_name)
        if not route or route.get("managed_by") != MANAGED_BY:
            hit["found"] = False
            return None  # nothing to write; leave file and intent untouched
        hit["found"] = True
        if grant:
            route["toolsets"] = [str(t).strip() for t in grant if str(t).strip()]
        else:
            route.pop("toolsets", None)
        subs[trigger_name] = route
        return (trigger_name, route)

    _serialized_route_write(_mutate)
    return hit["found"]


def unregister_route(trigger_name: str) -> bool:
    """Drop our managed route AND its intent row together (one txn): without
    the intent delete the reconciler would faithfully resurrect the route."""
    from . import store
    result = {"ret": True}

    def _mutate(subs: Dict[str, dict], conn) -> Optional[tuple]:
        route = subs.get(trigger_name)
        if route is None:
            pass  # already gone from the file: still clear any stale intent
        elif route.get("managed_by") != MANAGED_BY:
            result["ret"] = False  # never delete routes we didn't create
            return _KEEP  # sentinel: no file change, no intent change
        else:
            del subs[trigger_name]
        conn.execute("DELETE FROM route_intent WHERE name=?", (trigger_name,))
        return None

    _serialized_route_write(_mutate)
    return result["ret"]


def reconcile_routes(dry_run: bool = False) -> Dict[str, Any]:
    """Converge webhook_subscriptions.json back to the intent rows.

    The gateway owns that file; anything it (or a user) did to OUR routes is
    re-applied from SQLite intent — route deleted, secret rotated, toolsets
    edited, whatever the drift. Foreign-managed name collisions are reported,
    never overwritten. Managed routes with no intent row (pre-C5 installs)
    are adopted, so the reconciler has a baseline for everything we own.
    """
    from . import store
    intents = store.route_intents()
    subs = _load_subs()
    drifted: List[str] = []
    conflicts: List[str] = []
    for name, want in sorted(intents.items()):
        have = subs.get(name)
        if have is None or have != want:
            if have is not None and have.get("managed_by") not in (None, MANAGED_BY):
                conflicts.append(name)   # a user route took our name: doctor's job
                continue
            drifted.append(name)
    adopted: List[str] = []
    for name, have in sorted(subs.items()):
        if (isinstance(have, dict) and have.get("managed_by") == MANAGED_BY
                and name not in intents):
            adopted.append(name)
    repaired = 0
    if not dry_run:
        for name in drifted:
            want = dict(intents[name])
            def _mutate(subs_d: Dict[str, dict], conn, _n=name, _w=want) -> Optional[tuple]:
                subs_d[_n] = _w
                return (_n, _w)
            _serialized_route_write(_mutate)
            repaired += 1
        if adopted:
            for name in adopted:
                store.route_intent_set(name, subs[name])
    return {"ok": True, "drifted": drifted, "repaired": repaired,
            "adopted": adopted, "conflicts": conflicts}


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
