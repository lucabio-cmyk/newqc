"""CAPA (Corrective And Preventive Action) drafting.

Builds a CAPA record from a QC failure context and a ranked list of probable
root causes (produced by the knowledge graph). All fields that tests assert on
are *deterministic*: identical inputs always yield the same ``capa_number``,
``severity``, dates and action text. Wall-clock time is never used for those
fields — date offsets are computed from an ``incident_date`` supplied by the
caller (defaulting to a fixed epoch when absent).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Any

# Severity -> (target closure offset days, monitoring period days).
_SEVERITY_POLICY: dict[str, tuple[int, int]] = {
    "CRITICAL": (3, 30),
    "HIGH": (7, 30),
    "MEDIUM": (14, 14),
    "LOW": (30, 7),
}

# Westgard rules dominated by random error -> escalate severity.
_RANDOM_ERROR_RULES = {"1-3S", "R-4S"}
# Analytes where an out-of-control result carries high clinical risk.
_CRITICAL_ANALYTES = {"serology", "nat", "blood_gas", "cardiac", "coagulation"}


def _hash8(*parts: object) -> str:
    """Return a stable 8-char hex digest of the given parts."""
    payload = "|".join(str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8].upper()


def derive_severity(qc_context: dict[str, Any]) -> str:
    """Derive a CAPA severity from the QC failure context.

    Random-error rule violations and critical analytes escalate severity.
    """
    rule = (qc_context.get("westgard_rule_violated") or "").upper()
    analyte_type = (qc_context.get("analyte_type") or "").lower()

    if rule in _RANDOM_ERROR_RULES or analyte_type in _CRITICAL_ANALYTES:
        return "CRITICAL"
    if rule:  # any other (systematic) rule violation
        return "HIGH"
    if (qc_context.get("qc_status") or "").upper() == "FAIL":
        return "HIGH"
    return "MEDIUM"


def _parse_incident_date(incident_date: str | None) -> date:
    """Parse an ISO date/datetime string; fall back to a fixed epoch.

    A fixed fallback keeps date fields deterministic when the caller omits
    ``incident_date`` (so tests need not pin the wall clock).
    """
    if incident_date:
        try:
            return datetime.fromisoformat(incident_date).date()
        except ValueError:
            pass
    return date(1970, 1, 1)


def draft_capa(
    qc_context: dict[str, Any],
    ranked_causes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Draft a CAPA record from the QC context and ranked root causes.

    Args:
        qc_context: the QC failure context (analyte_code, analyte_type,
            westgard_rule_violated, qc_status, optional ``incident_date``...).
        ranked_causes: output of ``graph.infer`` (highest confidence first).

    Returns:
        A CAPA record dict with deterministic identifying fields.
    """
    analyte = qc_context.get("analyte_code") or "UNKNOWN"
    severity = derive_severity(qc_context)

    top = ranked_causes[0] if ranked_causes else None
    if top is not None:
        root_cause_category = top.get("category", "unknown")
        root_cause_description = top.get("cause", "Undetermined root cause")
        immediate_action = top.get(
            "corrective_action",
            "Investigate using a structured RCA and repeat QC after correction.",
        )
        preventive_action = top.get(
            "preventive_action",
            "Review SOPs and maintenance/calibration schedules.",
        )
        ai_rca_confidence = float(top.get("confidence", 0.0))
    else:
        root_cause_category = "unknown"
        root_cause_description = "Insufficient signal — perform structured RCA"
        immediate_action = (
            "Halt patient reporting for this analyte and perform a structured "
            "root-cause investigation; repeat QC after corrective action."
        )
        preventive_action = "Review SOPs and maintenance/calibration schedules."
        ai_rca_confidence = 0.0

    # Deterministic CAPA number from the salient inputs.
    rule = qc_context.get("westgard_rule_violated") or "NA"
    digest = _hash8(analyte, rule, qc_context.get("qc_status") or "NA", root_cause_category)
    capa_number = f"CAPA-{analyte}-{digest}"

    incident = _parse_incident_date(qc_context.get("incident_date"))
    closure_offset, monitoring_days = _SEVERITY_POLICY.get(severity, _SEVERITY_POLICY["MEDIUM"])
    target_closure_date = (incident + timedelta(days=closure_offset)).isoformat()

    return {
        "capa_number": capa_number,
        "status": "OPEN",
        "severity": severity,
        "analyte_code": analyte,
        "root_cause_category": root_cause_category,
        "root_cause_description": root_cause_description,
        "ai_suggested_causes": ranked_causes,
        "ai_rca_confidence": round(ai_rca_confidence, 4),
        "immediate_action": immediate_action,
        "preventive_action": preventive_action,
        "incident_date": incident.isoformat(),
        "target_closure_date": target_closure_date,
        "monitoring_period_days": monitoring_days,
    }
