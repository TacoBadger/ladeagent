"""Ladeplaner med menneske i loopet.

Princippet: modellen kan FORESLÅ en plan (status "pending"), men kan ikke
godkende den. Godkendelse er en menneskelig handling uden for modellen
(CLI: `python -m ladeagent.cli approve <plan_id>`). Det betyder at selv en
vellykket prompt injection højst kan skabe et forslag, aldrig en handling.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from ladeagent import config
from ladeagent.tools import ToolError, WindowModel


def _load() -> dict[str, dict]:
    if config.PLANS_FILE.exists():
        return json.loads(config.PLANS_FILE.read_text())
    return {}


def _save(plans: dict[str, dict]) -> None:
    config.PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.PLANS_FILE.write_text(json.dumps(plans, indent=2, ensure_ascii=False))


def create_charging_plan(area: str, start: str, end: str, kwh: float, max_kw: float, customer_note: str = "") -> dict:
    """Opretter et FORSLAG til ladeplan. Returnerer status 'pending'. Kun et menneske kan godkende."""
    p = WindowModel(area=area, start=start, end=end)
    s, e = p.bounds()
    if not (0 < kwh <= config.MAX_KWH) or not (0 < max_kw <= config.MAX_KW):
        raise ToolError("kwh/max_kw uden for tilladt interval.")
    plans = _load()
    plan_id = f"plan_{uuid.uuid4().hex[:8]}"
    plans[plan_id] = {
        "plan_id": plan_id, "status": "pending", "area": p.area, "start": s.isoformat(timespec="minutes"),
        "end": e.isoformat(timespec="minutes"), "kwh": kwh, "max_kw": max_kw,
        "customer_note": customer_note[:200], "created_at": datetime.now().isoformat(timespec="seconds"),
        "approved_at": None,
    }
    _save(plans)
    return {
        "plan_id": plan_id, "status": "pending",
        "message": "Planen er oprettet som forslag og afventer kundens godkendelse i appen. Den er IKKE aktiv endnu, og du kan ikke aktivere den.",
        "plan": plans[plan_id],
    }


def get_plan_status(plan_id: str) -> dict:
    plans = _load()
    if plan_id not in plans:
        raise ToolError(f"Ingen plan med id {plan_id!r}.")
    return plans[plan_id]


def approve_plan(plan_id: str, actor: str = "human-cli") -> dict:
    """Menneskelig godkendelse. Eksponeres IKKE som tool til modellen."""
    plans = _load()
    if plan_id not in plans:
        raise ToolError(f"Ingen plan med id {plan_id!r}.")
    plans[plan_id]["status"] = "approved"
    plans[plan_id]["approved_at"] = datetime.now().isoformat(timespec="seconds")
    plans[plan_id]["approved_by"] = actor
    _save(plans)
    return plans[plan_id]


WRITE_TOOLS: dict[str, dict] = {
    "create_charging_plan": {
        "fn": create_charging_plan,
        "description": "Opretter et FORSLAG til en ladeplan for kunden (status: pending). Planen bliver først aktiv når kunden selv godkender den i appen; "
                       "du kan ikke godkende, aktivere eller ændre status. Kald kun dette tool når kunden eksplicit har bedt om at få oprettet en plan.",
        "schema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "enum": list(config.AREAS)},
                "start": {"type": "string", "description": "Ladestart, lokal tid, ISO-8601."},
                "end": {"type": "string", "description": "Ladeslut (eksklusiv), lokal tid, ISO-8601."},
                "kwh": {"type": "number"},
                "max_kw": {"type": "number"},
                "customer_note": {"type": "string", "description": "Kort note fra kunden, max 200 tegn."},
            },
            "required": ["area", "start", "end", "kwh", "max_kw"],
            "additionalProperties": False,
        },
    },
    "get_plan_status": {
        "fn": get_plan_status,
        "description": "Slår status op på en ladeplan (pending/approved).",
        "schema": {"type": "object", "properties": {"plan_id": {"type": "string"}}, "required": ["plan_id"], "additionalProperties": False},
    },
}
