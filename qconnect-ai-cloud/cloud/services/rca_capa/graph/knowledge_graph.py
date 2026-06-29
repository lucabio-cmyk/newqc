"""In-memory clinical-lab RCA knowledge graph (pure stdlib).

A small directed, weighted graph linking *Symptom* nodes (observable QC facts:
Westgard rule violated, shift-vs-trend pattern, analyte type, reagent lot age,
control material status, environmental flags) to probable *Cause* nodes
(reagent deterioration, calibration drift, instrument maintenance due, operator
technique, control material expiry, environmental conditions), each of which is
attached to a recommended *corrective* and *preventive* Action.

Inference is fully transparent: every cause accumulates the weights of the
symptom edges that fired, the total is normalised against the cause's maximum
achievable score, and causes are ranked by the resulting confidence in
``[0, 1]``. There is no ML and no randomness, so results are deterministic.

The graph models *evidence* as ``(symptom_key, predicate, weight)`` edges where
``predicate`` is a callable evaluated against the supplied symptom dict. This
keeps the clinical ruleset declarative and easy to audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# A predicate inspects the raw symptom value and returns True when the edge
# fires. ``None`` (missing symptom) never fires.
Predicate = Callable[[Any], bool]


def _eq(*expected: object) -> Predicate:
    """Edge fires when the symptom value equals any of ``expected``."""
    wanted = {str(e).upper() for e in expected}

    def _check(value: Any) -> bool:
        return value is not None and str(value).upper() in wanted

    return _check


def _truthy() -> Predicate:
    """Edge fires when the symptom value is truthy."""

    def _check(value: Any) -> bool:
        return bool(value)

    return _check


def _gte(threshold: float) -> Predicate:
    """Edge fires when a numeric symptom value is >= ``threshold``."""

    def _check(value: Any) -> bool:
        try:
            return value is not None and float(value) >= threshold
        except (TypeError, ValueError):
            return False

    return _check


@dataclass(frozen=True)
class Edge:
    """A weighted, predicated symptom -> cause evidence edge."""

    symptom: str
    predicate: Predicate
    weight: float
    rationale: str


@dataclass
class Cause:
    """A probable root cause with its evidence edges and recommended actions."""

    key: str
    name: str
    category: str
    description: str
    corrective_action: str
    preventive_action: str
    edges: list[Edge] = field(default_factory=list)

    @property
    def max_score(self) -> float:
        """Maximum achievable evidence score (sum of all edge weights)."""
        return sum(e.weight for e in self.edges) or 1.0


# --------------------------------------------------------------------------- #
# Westgard rule families (used by predicates below)
# --------------------------------------------------------------------------- #
# Random-error-dominant rules: a single point far out or a within-run range
# violation. These point at sporadic causes (bad aliquot, bubble, transient
# instrument fault, deteriorating reagent).
RANDOM_ERROR_RULES = ("1-3S", "R-4S")
# Systematic-error rules: shifts and trends across runs. These point at
# calibration drift, reagent-lot bias, ageing components.
SYSTEMATIC_ERROR_RULES = ("2-2S", "4-1S", "10X", "10x", "8X", "7T", "6X", "3-1S")


class KnowledgeGraph:
    """In-memory weighted RCA knowledge graph with transparent scoring."""

    backend_name = "in-memory"

    def __init__(self) -> None:
        self._causes: list[Cause] = _seed_causes()

    # ------------------------------------------------------------------ #
    # Introspection helpers
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """The in-memory graph is always available."""
        return True

    def causes(self) -> list[Cause]:
        """Return the seeded causes (read-only view)."""
        return list(self._causes)

    # ------------------------------------------------------------------ #
    # Inference
    # ------------------------------------------------------------------ #
    def infer(self, symptoms: dict[str, Any]) -> list[dict[str, Any]]:
        """Rank probable root causes for the given symptom dict.

        Args:
            symptoms: observable facts, e.g.::

                {
                    "westgard_rule_violated": "1-3S",
                    "analyte_type": "serology",
                    "shift_or_trend": "shift",
                    "reagent_lot_age_days": 75,
                    "control_expired": False,
                }

        Returns:
            A list of ranked causes (highest confidence first), each a dict::

                {
                    "cause": str,
                    "category": str,
                    "confidence": float,   # 0..1
                    "evidence": list[str],
                    "corrective_action": str,
                    "preventive_action": str,
                }

            Only causes with at least one matched edge are returned.
        """
        ranked: list[dict[str, Any]] = []
        for cause in self._causes:
            score = 0.0
            evidence: list[str] = []
            for edge in cause.edges:
                value = symptoms.get(edge.symptom)
                if edge.predicate(value):
                    score += edge.weight
                    evidence.append(edge.rationale)
            if score <= 0.0:
                continue
            confidence = round(min(1.0, score / cause.max_score), 4)
            ranked.append(
                {
                    "cause": cause.name,
                    "category": cause.category,
                    "confidence": confidence,
                    "evidence": evidence,
                    "corrective_action": cause.corrective_action,
                    "preventive_action": cause.preventive_action,
                }
            )

        # Rank by confidence desc; break ties deterministically by cause name.
        ranked.sort(key=lambda c: (-c["confidence"], c["cause"]))
        return ranked


def _seed_causes() -> list[Cause]:
    """Seed a realistic clinical-lab RCA ruleset (>=6 causes)."""
    return [
        # ----------------------------------------------------------------- #
        # 1. Reagent deterioration (random/systematic, lot-age driven)
        # ----------------------------------------------------------------- #
        Cause(
            key="reagent_deterioration",
            name="Reagent deterioration",
            category="reagent",
            description=(
                "Reagent has degraded (heat/light exposure, freeze-thaw, or "
                "age past optimal performance), shifting recovered values."
            ),
            corrective_action=(
                "Open a fresh reagent vial/cartridge from a verified lot, "
                "re-run controls, and confirm recovery before resuming "
                "patient testing."
            ),
            preventive_action=(
                "Enforce first-in-first-out reagent rotation, log on-board and "
                "open-vial stability, and alert when lots approach expiry."
            ),
            edges=[
                Edge("reagent_lot_age_days", _gte(60), 0.45, "Reagent lot older than 60 days"),
                Edge("reagent_lot_age_days", _gte(90), 0.25, "Reagent lot older than 90 days"),
                Edge(
                    "westgard_rule_violated",
                    _eq(*RANDOM_ERROR_RULES),
                    0.20,
                    "Random-error rule consistent with a failing reagent",
                ),
                Edge(
                    "shift_or_trend",
                    _eq("trend"),
                    0.20,
                    "Drifting trend consistent with progressive reagent ageing",
                ),
                Edge(
                    "new_reagent_lot",
                    _truthy(),
                    0.30,
                    "Recently introduced reagent lot (possible lot bias)",
                ),
            ],
        ),
        # ----------------------------------------------------------------- #
        # 2. Calibration drift (systematic shift/trend)
        # ----------------------------------------------------------------- #
        Cause(
            key="calibration_drift",
            name="Calibration drift",
            category="calibration",
            description=(
                "The calibration curve has drifted from its assigned setpoint, "
                "producing a sustained systematic bias across runs."
            ),
            corrective_action=(
                "Recalibrate the assay with fresh calibrators, then verify with "
                "controls across the reportable range before releasing results."
            ),
            preventive_action=(
                "Tighten the recalibration schedule, trend calibration metrics "
                "(slope/intercept) over time, and recalibrate proactively on "
                "reagent-lot changes."
            ),
            edges=[
                Edge(
                    "westgard_rule_violated",
                    _eq(*SYSTEMATIC_ERROR_RULES),
                    0.45,
                    "Systematic-error Westgard rule indicates a sustained shift",
                ),
                Edge(
                    "shift_or_trend",
                    _eq("trend"),
                    0.35,
                    "Progressive trend is a hallmark of calibration drift",
                ),
                Edge(
                    "shift_or_trend",
                    _eq("shift"),
                    0.25,
                    "Level shift consistent with a calibration step change",
                ),
                Edge(
                    "days_since_calibration",
                    _gte(30),
                    0.25,
                    "More than 30 days since last calibration",
                ),
            ],
        ),
        # ----------------------------------------------------------------- #
        # 3. Instrument maintenance due (ageing components)
        # ----------------------------------------------------------------- #
        Cause(
            key="instrument_maintenance_due",
            name="Instrument maintenance due",
            category="instrument",
            description=(
                "An instrument component (lamp, electrode, pump, optics) is "
                "ageing or fouled, or scheduled maintenance is overdue."
            ),
            corrective_action=(
                "Perform the due maintenance procedure (clean/replace the "
                "affected module), run a system check, then repeat QC."
            ),
            preventive_action=(
                "Adhere to the preventive-maintenance calendar and monitor "
                "instrument health flags / error logs for early degradation."
            ),
            edges=[
                Edge(
                    "maintenance_overdue",
                    _truthy(),
                    0.45,
                    "Preventive maintenance is overdue",
                ),
                Edge(
                    "instrument_error_flag",
                    _truthy(),
                    0.30,
                    "Instrument reported an error/health flag",
                ),
                Edge(
                    "westgard_rule_violated",
                    _eq(*RANDOM_ERROR_RULES),
                    0.25,
                    "Random-error rule consistent with a transient instrument fault",
                ),
                Edge(
                    "shift_or_trend",
                    _eq("trend"),
                    0.20,
                    "Slow trend consistent with component ageing",
                ),
            ],
        ),
        # ----------------------------------------------------------------- #
        # 4. Operator technique (sporadic random error)
        # ----------------------------------------------------------------- #
        Cause(
            key="operator_technique",
            name="Operator technique",
            category="operator",
            description=(
                "A handling error such as mis-pipetting, an air bubble, a "
                "short sample, or an incorrect control reconstitution."
            ),
            corrective_action=(
                "Re-run the control with careful technique by a competent "
                "operator; verify pipette calibration and sample integrity."
            ),
            preventive_action=(
                "Reinforce competency assessment and SOP adherence, and "
                "schedule periodic pipette calibration checks."
            ),
            edges=[
                Edge(
                    "westgard_rule_violated",
                    _eq("1-3S", "R-4S"),
                    0.40,
                    "Single large outlier / range rule typical of a handling error",
                ),
                Edge(
                    "shift_or_trend",
                    _eq("single", "outlier", "none"),
                    0.25,
                    "Isolated point rather than a sustained pattern",
                ),
                Edge(
                    "recent_operator_change",
                    _truthy(),
                    0.25,
                    "Recent change of operator / new operator on shift",
                ),
            ],
        ),
        # ----------------------------------------------------------------- #
        # 5. Control material expiry / mishandling
        # ----------------------------------------------------------------- #
        Cause(
            key="control_material_expiry",
            name="Control material expiry or mishandling",
            category="control_material",
            description=(
                "The QC material itself is expired, a fresh control lot has not "
                "had ranges established, or it was reconstituted/stored "
                "incorrectly."
            ),
            corrective_action=(
                "Replace with in-date, correctly stored control material and "
                "re-run; for a new control lot, establish/verify the mean and SD."
            ),
            preventive_action=(
                "Track control-lot expiry and storage conditions, and run "
                "parallel testing when transitioning to a new control lot."
            ),
            edges=[
                Edge("control_expired", _truthy(), 0.50, "Control material is expired"),
                Edge(
                    "new_control_lot",
                    _truthy(),
                    0.30,
                    "New control lot without established ranges",
                ),
                Edge(
                    "shift_or_trend",
                    _eq("shift"),
                    0.20,
                    "Step shift consistent with a control-lot change",
                ),
            ],
        ),
        # ----------------------------------------------------------------- #
        # 6. Environmental conditions
        # ----------------------------------------------------------------- #
        Cause(
            key="environmental",
            name="Environmental conditions",
            category="environment",
            description=(
                "Ambient temperature or humidity excursions (or unstable mains "
                "power) are affecting reagent/instrument performance."
            ),
            corrective_action=(
                "Restore environmental conditions to spec, allow the system to "
                "equilibrate, then repeat QC."
            ),
            preventive_action=(
                "Continuously monitor and alarm laboratory temperature/humidity "
                "and maintain HVAC and UPS on the analyser."
            ),
            edges=[
                Edge(
                    "environment_excursion",
                    _truthy(),
                    0.50,
                    "Recorded temperature/humidity excursion",
                ),
                Edge(
                    "shift_or_trend",
                    _eq("trend"),
                    0.30,
                    "Gradual trend consistent with a drifting environment",
                ),
                Edge(
                    "westgard_rule_violated",
                    _eq("7T", "6X", "8X"),
                    0.20,
                    "Trend rule consistent with environmental drift",
                ),
            ],
        ),
    ]
