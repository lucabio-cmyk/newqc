"""Lightweight explainability + action synthesis for ML predictions.

The :class:`Explainer` turns the failure predictor's feature contributions into
a SHAP-like *additive attribution* (no ``shap`` dependency): each feature's
signed contribution to the prediction logit is reported, and contributions sum
(with the model bias) back to the logit — the defining property of an additive
feature-attribution method.

It also synthesizes:

* a one-line ``recommended_action`` from the top drivers, the risk level, and
  the anomaly verdict; and
* a ``clinical_impact_percent`` estimate that maps the failure probability and
  the analyte's clinical criticality (serology / NAT are weighted higher because
  a missed infectious-disease QC failure carries patient-safety risk) onto a
  0..100 scale.
"""

from __future__ import annotations

# Clinical criticality multipliers by analyte discipline. Serology and NAT
# (infectious-disease screening) carry the highest patient-safety impact when a
# QC failure goes undetected.
_CRITICALITY: dict[str, float] = {
    "serology": 1.0,
    "nat": 1.0,
    "coagulation": 0.8,
    "hematology": 0.7,
    "chemistry": 0.6,
}
_DEFAULT_CRITICALITY = 0.6

# Human-readable phrasing for each heuristic feature when it is a top driver.
_FEATURE_PHRASES: dict[str, str] = {
    "zscore": "current value far from target",
    "trend": "sustained drift trend in recent results",
    "drift": "process mean shifted from target",
    "variance": "increasing result variability",
}


class Explainer:
    """Produce additive attributions, a clinical-impact estimate and an action."""

    criticality = _CRITICALITY

    def attribute(self, contributing_factors: list[dict]) -> list[dict]:
        """Return a SHAP-like additive attribution list.

        Input is the failure predictor's ``contributing_factors`` (each with a
        signed ``contribution``); output is ``[{feature, contribution}]`` ranked
        by absolute contribution.
        """
        attributions = [
            {"feature": f["name"], "contribution": round(float(f["contribution"]), 4)}
            for f in contributing_factors
        ]
        attributions.sort(key=lambda a: abs(a["contribution"]), reverse=True)
        return attributions

    def clinical_impact_percent(self, failure_probability: float, analyte_type: str) -> float:
        """Estimate clinical impact (0..100) from probability + criticality.

        Higher-criticality disciplines (serology / NAT) scale the same failure
        probability to a larger clinical impact.
        """
        crit = self.criticality.get((analyte_type or "").lower(), _DEFAULT_CRITICALITY)
        impact = float(failure_probability) * crit * 100.0
        return round(max(0.0, min(100.0, impact)), 2)

    def recommended_action(
        self,
        risk_level: str,
        attributions: list[dict],
        anomaly_detected: bool,
        clinical_impact_percent: float,
    ) -> str:
        """Synthesize a one-line recommended action from the top drivers."""
        top = attributions[0]["feature"] if attributions else None
        driver_phrase = _FEATURE_PHRASES.get(top, "deviation in recent QC behaviour")

        if risk_level == "critical":
            base = (
                "CRITICAL: high 48h QC failure risk — halt patient testing for this "
                "analyte and perform recalibration / maintenance now"
            )
        elif risk_level == "high":
            base = (
                "HIGH risk of QC failure within 48h — schedule recalibration and "
                "review reagent/lot before the next run"
            )
        elif risk_level == "medium":
            base = "MEDIUM risk — increase QC monitoring frequency and inspect for " "early drift"
        else:
            base = "LOW risk — continue routine QC monitoring"

        detail = f"primary driver: {driver_phrase}"
        if anomaly_detected:
            detail += "; current point flagged as an anomaly"
        if clinical_impact_percent >= 50.0:
            detail += f"; estimated clinical impact {clinical_impact_percent:.0f}%"

        return f"{base} ({detail})."

    def explain(
        self,
        prediction: dict,
        anomaly: dict,
        analyte_type: str,
    ) -> dict:
        """Bundle attributions, clinical impact and a recommended action.

        Returns ``{attributions, clinical_impact_percent, recommended_action}``.
        """
        attributions = self.attribute(prediction.get("contributing_factors", []))
        impact = self.clinical_impact_percent(
            prediction.get("failure_probability_48h", 0.0), analyte_type
        )
        action = self.recommended_action(
            prediction.get("failure_risk_level", "low"),
            attributions,
            bool(anomaly.get("anomaly_detected", False)),
            impact,
        )
        return {
            "attributions": attributions,
            "clinical_impact_percent": impact,
            "recommended_action": action,
        }
