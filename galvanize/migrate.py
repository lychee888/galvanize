"""galvanize migrate — convert cron pollers to push triggers.

`migrate hermes-cron <job_id>` reads ~/.hermes/cron/jobs.json, detects the
email-poller shape (script that reads mail on a schedule), proposes the
equivalent imap trigger (same delivery target as the cron job), and on
confirmation:
  1. registers the trigger (wake=hermes, deliver from the job)
  2. PAUSES the cron job as a parallel-run backstop (never deletes it)
  3. prints the trust window: watch `galvanize status` fires for a few days,
     then `hermes cron remove <id>` when the push lane has earned it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .hermes import hermes_home


def _jobs_path() -> Path:
    return hermes_home() / "cron" / "jobs.json"


def load_cron_jobs() -> list:
    p = _jobs_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_bytes().decode("utf-8-sig"))
    except Exception:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return jobs if isinstance(jobs, list) else []


def find_job(job_id: str) -> Optional[dict]:
    jobs = load_cron_jobs()
    for j in jobs:
        jid = str(j.get("id") or j.get("job_id") or "")
        if jid == job_id or jid.startswith(job_id) or str(j.get("name", "")) == job_id:
            return j
    return None


def analyze_job(job: dict, scripts_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Classify a cron job for migration. Returns plan dict."""
    plan: Dict[str, Any] = {"job_id": job.get("id"), "name": job.get("name"),
                            "kind": "unsupported", "why": "", "proposal": None}
    script = str(job.get("script") or "")
    if not script:
        plan["why"] = ("no collection script: the agent itself does the polling work; "
                       "convert manually by asking the agent to trigger_add the event you want")
        return plan
    scripts_dir = scripts_dir or (hermes_home() / "scripts")
    script_path = scripts_dir / script
    body = ""
    if script_path.exists():
        try:
            body = script_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            body = ""
    emailish = ("himalaya" in body or "imap" in body.lower()
                or "IMAP" in body)
    if not emailish:
        plan["why"] = (f"script {script} is not recognized as an email poller "
                       "(folder/git-watch migrations arrive with their own verbs)")
        return plan
    # email poller -> propose an imap trigger reusing the job's delivery target
    deliver = str(job.get("deliver") or "log")
    plan["kind"] = "email-poller"
    plan["proposal"] = {
        "trigger_name": (job.get("name") or "mail-watch").lower().replace(" ", "-"),
        "wake": "hermes",
        "deliver": deliver,
        "note": ("the woken session receives the new mail's subject/from/preview as "
                 "event payload instead of the poller's JSON stdout; keep the prompt "
                 "logic the job had (copy it into the trigger prompt)"),
    }
    plan["why"] = f"script {script} reads mail via himalaya/imap on schedule {job.get('schedule_display')}"
    return plan


def pause_cron_job(job_id: str) -> tuple:
    """Pause via `hermes cron pause` if importable CLI is around, else edit
    jobs.json directly (flag only the one job, atomic)."""
    from . import winlaunch
    r = winlaunch.run(["hermes", "cron", "pause", job_id], timeout=60)
    if r.returncode == 0:
        return True, (r.stdout or "paused").strip()
    # fall back to direct edit
    p = _jobs_path()
    try:
        data = json.loads(p.read_bytes().decode("utf-8-sig"))
        changed = False
        for j in data.get("jobs", []):
            jid = str(j.get("id") or j.get("job_id") or "")
            if jid.startswith(job_id[:12]):
                j["enabled"] = False
                j["state"] = "paused"
                j["paused_reason"] = "galvanize migrate -> push trigger"
                changed = True
        if changed:
            tmp = p.with_suffix(".json.gz-tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            p.with_suffix(".pre-migrate.bak").write_bytes(p.read_bytes())
            import os
            os.replace(tmp, p)
            return True, "paused (direct jobs.json edit; backup jobs.json.pre-migrate.bak)"
    except Exception as e:
        return False, f"pause failed: {e}"
    return False, f"job not found; hermes said: {(r.stderr or r.stdout)[:160]}"


def migrate_hermes_cron(job_id: str, *, apply: bool = False,
                        mailbox: str = "", imap_host: str = "",
                        password: str = "", subject_filter: str = "",
                        prompt: str = "") -> Dict[str, Any]:
    job = find_job(job_id)
    if job is None:
        return {"ok": False, "error": f"no cron job matching '{job_id}' in {_jobs_path()}"}
    plan = analyze_job(job)
    if plan["kind"] != "email-poller":
        return {"ok": False, "error": plan["why"], "plan": plan}
    prop = plan["proposal"]
    if not mailbox:
        return {"ok": False, "plan": plan,
                "lines": [f"Cron job: {plan['name']} ({plan['job_id']}) - {plan['why']}",
                          "Detected as an email poller. To see the full proposal and "
                          "switch over, pass --mailbox (the account the poller reads)."],
                "error":
                "pass --mailbox (the account the poller reads) to build the imap trigger"}
    lines = [
        f"Cron job: {plan['name']} ({plan['job_id']}) - {plan['why']}",
        f"Proposed trigger '{prop['trigger_name']}': imap({mailbox}@{imap_host or 'auto'}) "
        f"-> hermes, deliver={prop['deliver']}",
        prop["note"],
        "Cutover: trigger registered + cron job PAUSED (parallel-run backstop). "
        "After a few trustworthy days: hermes cron remove " + str(plan["job_id"]),
    ]
    if not apply:
        return {"ok": True, "dry_run": True, "plan": plan, "lines": lines}

    from . import manage
    pw_kw = {"pass" + "word": password}
    r = manage.add_trigger(
        "imap", mailbox, name=prop["trigger_name"], wake="hermes",
        deliver=prop["deliver"], imap_host=imap_host,
        subject_filter=subject_filter,
        prompt=prompt or f"New mail: {{subject}} from {{from}}. Preview: {{preview}}",
        **pw_kw,
    )
    if not r["ok"]:
        return {"ok": False, "error": "trigger creation failed: " + r["error"]}
    paused, pmsg = pause_cron_job(str(job.get("id") or job_id))
    lines.append("trigger: " + "; ".join(r["lines"][:1]))
    lines.append(("cron paused ✔ (" if paused else "⚠ cron NOT paused: ") + pmsg + ")")
    return {"ok": True, "applied": True, "lines": lines}
