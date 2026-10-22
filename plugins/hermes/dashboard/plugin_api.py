"""Galvanize dashboard plugin — backend API routes.

Mounted at /api/plugins/galvanize/ by the dashboard plugin system
(auth: the dashboard's session-token middleware covers /api/plugins/*).

Intentionally thin: every handler calls galvanize.manage directly — the
same ops the CLI, the agent tools, and the serve API use, so the four
surfaces cannot drift. This is IN-PROCESS (the dashboard does not go
through `galvanize serve`; two doors, one lock).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()


def _manage():
    try:
        from galvanize import manage
        return manage
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="galvanize is not installed in the Hermes interpreter: pip install galvanize",
        )


@router.get("/status")
def status() -> Dict[str, Any]:
    return _manage().status()


@router.get("/doctor")
def doctor() -> Dict[str, Any]:
    return _manage().doctor()


@router.post("/test")
def test(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    return _manage().test_trigger(name, body.get("payload"))


@router.post("/toggle")
def toggle(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    from galvanize.config import set_enabled
    from galvanize.store import StaleRevisionError
    name = str(body.get("name", "")).strip()
    enabled = bool(body.get("enabled", True))
    exp = body.get("expected_revision")
    try:
        ok = set_enabled(name, enabled,
                         expected_revision=(int(exp) if exp is not None else None))
    except StaleRevisionError as e:
        # C5: the dashboard's snapshot went stale — 409, never a silent clobber.
        raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"no trigger '{name}'")
    return {"ok": True, "name": name, "enabled": enabled}


@router.post("/remove")
def remove(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    name = str(body.get("name", "")).strip()
    r = _manage().remove_trigger_op(name)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error", "remove failed"))
    return r
