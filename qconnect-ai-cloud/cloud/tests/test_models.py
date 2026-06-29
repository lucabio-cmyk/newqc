"""Unit tests for the deterministic QC engines.

Pure-Python, no DB / network / scipy required. These verify the actual rule
logic with crafted histories.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the engines importable in either layout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICE_DIR = _REPO_ROOT / "cloud" / "services" / "qc_evaluation"
for _p in (_REPO_ROOT, _SERVICE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cloud.services.qc_evaluation.models import (  # noqa: E402
    DistributionDetector,
    QConnectEngine,
    SigmaEngine,
    WestgardEngine,
)


# --------------------------------------------------------------------------- #
# Westgard
# --------------------------------------------------------------------------- #
def test_westgard_pass() -> None:
    """A value within +/-1 SD with a stable history passes."""
    eng = WestgardEngine()
    history = [100.0, 101.0, 99.0, 100.5, 99.5]
    res = eng.evaluate(value=100.2, target=100.0, sd=2.0, history=history)
    assert res["status"] == "PASS"
    assert res["rule_violated"] is None


def test_westgard_1_3s_violation() -> None:
    """A value beyond +3 SD trips 1-3S -> FAIL."""
    eng = WestgardEngine()
    res = eng.evaluate(value=107.0, target=100.0, sd=2.0, history=[100.0, 101.0])
    assert res["status"] == "FAIL"
    assert res["rule_violated"] == "1-3S"
    assert res["deviation_sd"] > 3.0


def test_westgard_2_2s_violation() -> None:
    """Two consecutive values beyond the same +2 SD trip 2-2S -> FAIL."""
    eng = WestgardEngine()
    # Previous value at +2.5 SD, new value at +2.5 SD, neither beyond 3 SD.
    history = [100.0, 105.0]  # last hist point = +2.5 SD
    res = eng.evaluate(value=105.0, target=100.0, sd=2.0, history=history)
    assert res["status"] == "FAIL"
    assert res["rule_violated"] == "2-2S"


def test_westgard_10x_violation() -> None:
    """Ten consecutive values on the same side of the mean trip 10x."""
    eng = WestgardEngine()
    # 9 prior points all slightly above the mean + the new one = 10 above.
    history = [100.5] * 9
    res = eng.evaluate(value=100.5, target=100.0, sd=2.0, history=history)
    assert res["status"] == "FAIL"
    assert res["rule_violated"] == "10x"


def test_westgard_1_2s_warning() -> None:
    """A single value just beyond +2 SD (not +3) is a REVIEW warning."""
    eng = WestgardEngine()
    res = eng.evaluate(value=104.5, target=100.0, sd=2.0, history=[100.0])
    assert res["status"] == "REVIEW_REQUIRED"
    assert res["rule_violated"] == "1-2S"


def test_westgard_stats() -> None:
    """calculate_stats returns sensible mean/sd/cv."""
    eng = WestgardEngine()
    stats = eng.calculate_stats([10.0, 12.0, 14.0])
    assert abs(stats["mean"] - 12.0) < 1e-9
    assert stats["sd"] > 0
    assert stats["cv"] > 0


# --------------------------------------------------------------------------- #
# Sigma
# --------------------------------------------------------------------------- #
def test_sigma_categorize_thresholds() -> None:
    """categorize maps each band to the right label."""
    eng = SigmaEngine()
    assert eng.categorize(6.5)[0] == ">6"
    assert eng.categorize(6.0)[0] == ">6"
    assert eng.categorize(5.0)[0] == "4-6"
    assert eng.categorize(3.5)[0] == "3-4"
    assert eng.categorize(2.5)[0] == "2-3"
    assert eng.categorize(1.0)[0] == "<2"


def test_sigma_calculate() -> None:
    """sigma = (TEa - |bias|) / CV."""
    eng = SigmaEngine()
    sigma = eng.calculate_sigma(bias_percent=2.0, cv_percent=2.0, allowable_error_percent=10.0)
    assert abs(sigma - 4.0) < 1e-9


def test_diagnostic_sigma() -> None:
    """Diagnostic sigma rewards weighting false negatives more heavily."""
    eng = SigmaEngine()
    res = eng.diagnostic_sigma(tp=95, tn=900, fp=3, fn=2, weight_fn=10.0, weight_fp=1.0)
    assert 0.0 <= res["sensitivity"] <= 1.0
    assert 0.0 <= res["specificity"] <= 1.0
    assert 0.0 <= res["diagnostic_sigma"] <= 6.0


# --------------------------------------------------------------------------- #
# QConnect
# --------------------------------------------------------------------------- #
def test_qconnect_percentiles() -> None:
    """Percentiles are monotonic and bracket the median."""
    eng = QConnectEngine()
    hist = [float(i) for i in range(1, 101)]  # 1..100
    p = eng.calculate_percentiles(hist)
    assert p["p5"] < p["p25"] < p["p50"] < p["p75"] < p["p95"]
    assert abs(p["p50"] - 50.5) < 1.0


def test_qconnect_evaluate_fail_outside_limits() -> None:
    """A value well outside the empirical limits fails."""
    eng = QConnectEngine()
    hist = [1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 1.02, 0.98, 1.0, 1.0]
    res = eng.evaluate(value=5.0, limits={"history": hist})
    assert res["status"] == "FAIL"
    assert res["ucl"] >= res["lcl"]


def test_qconnect_discordance() -> None:
    """compare_with_westgard flags disagreement."""
    eng = QConnectEngine()
    out = eng.compare_with_westgard({"status": "PASS"}, {"status": "FAIL"})
    assert out["discordance_detected"] is True


# --------------------------------------------------------------------------- #
# Distribution
# --------------------------------------------------------------------------- #
def test_distribution_gaussian() -> None:
    """A symmetric, unimodal sample is recommended for Westgard."""
    det = DistributionDetector()
    # Deterministic near-gaussian sample (symmetric around 0).
    hist = [-3, -2, -2, -1, -1, -1, 0, 0, 0, 0, 1, 1, 1, 2, 2, 3]
    hist = [float(x) for x in hist] * 3
    res = det.detect(hist)
    assert res["recommended_qc_approach"] == "westgard"
    assert res["detected_distribution"] in ("gaussian", "skewed")


def test_distribution_bimodal() -> None:
    """A clearly bimodal sample is recommended for QConnect."""
    det = DistributionDetector()
    low = [0.0, 0.1, 0.2, 0.1, 0.0, 0.15, 0.05] * 4
    high = [10.0, 10.1, 9.9, 10.2, 9.8, 10.05, 9.95] * 4
    res = det.detect(low + high)
    assert res["recommended_qc_approach"] == "qconnect"
    assert res["bimodal_score"] > 0.555
