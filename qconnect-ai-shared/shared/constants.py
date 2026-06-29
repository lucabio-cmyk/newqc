"""Domain constants shared across QConnect-AI services."""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Westgard multirules
# --------------------------------------------------------------------------- #
# Canonical rule identifiers. Order matters for reporting (most-specific first).
WESTGARD_RULES: tuple[str, ...] = (
    "1-3S",  # one result beyond +/-3 SD  -> random error, reject
    "2-2S",  # two consecutive beyond same +/-2 SD -> systematic error, reject
    "R-4S",  # range of two consecutive >= 4 SD -> random error, reject
    "4-1S",  # four consecutive beyond same +/-1 SD -> systematic error, reject
    "10x",  # ten consecutive on the same side of the mean -> systematic error
    "7T",  # seven consecutive trending up or down -> systematic drift
    "1-2S",  # warning only (gates the rejection rules)
)

# Rules that, when violated, indicate random vs systematic error.
RANDOM_ERROR_RULES: frozenset[str] = frozenset({"1-3S", "R-4S"})
SYSTEMATIC_ERROR_RULES: frozenset[str] = frozenset({"2-2S", "4-1S", "10x", "7T"})


# --------------------------------------------------------------------------- #
# Six Sigma categories (sigma metric -> category label + recommended rule set)
# --------------------------------------------------------------------------- #
SIGMA_CATEGORIES: tuple[tuple[float, str, str], ...] = (
    # (lower_bound_inclusive, label, recommended_rules)
    (6.0, ">6", "1-3S (N=2)"),
    (4.0, "4-6", "1-3S / 2-2S / R-4S (N=2)"),
    (3.0, "3-4", "1-3S / 2-2S / R-4S / 4-1S (N=4)"),
    (2.0, "2-3", "1-3S / 2-2S / R-4S / 4-1S / 8x (N=4, multi-rule)"),
    (0.0, "<2", "Method not fit for purpose - investigate"),
)


def sigma_category(sigma: float) -> tuple[str, str]:
    """Return ``(label, recommended_rules)`` for a sigma metric."""
    for lower, label, rules in SIGMA_CATEGORIES:
        if sigma >= lower:
            return label, rules
    return "<2", "Method not fit for purpose - investigate"


# --------------------------------------------------------------------------- #
# Distribution detection
# --------------------------------------------------------------------------- #
DISTRIBUTION_GAUSSIAN = "gaussian"
DISTRIBUTION_LOGNORMAL = "lognormal"
DISTRIBUTION_BIMODAL = "bimodal"
DISTRIBUTION_SKEWED = "skewed"
DISTRIBUTION_UNKNOWN = "unknown"

# Shapiro-Wilk p-value below which we reject the normality assumption.
SHAPIRO_WILK_ALPHA = 0.05
# Minimum samples required before any distribution claim is trustworthy.
MIN_SAMPLES_FOR_DISTRIBUTION = 30


# --------------------------------------------------------------------------- #
# QConnect percentile limits
# --------------------------------------------------------------------------- #
QCONNECT_LOWER_PERCENTILE = 5.0
QCONNECT_UPPER_PERCENTILE = 95.0


# --------------------------------------------------------------------------- #
# LOINC mapping for common serology analytes (illustrative subset).
# --------------------------------------------------------------------------- #
LOINC_MAP: dict[str, str] = {
    "HIV-AB": "75622-1",
    "HCV-AB": "13955-0",
    "HBSAG": "5196-1",
    "SYPHILIS": "20507-0",
    "TROPONIN-I": "10839-9",
    "TROPONIN-T": "6598-7",
}


# --------------------------------------------------------------------------- #
# Operational defaults
# --------------------------------------------------------------------------- #
DEFAULT_SYNC_INTERVAL_SECONDS = 300
DEFAULT_BATCH_SIZE = 100
DEFAULT_RETRY_MAX_ATTEMPTS = 10
DEFAULT_RETRY_BASE_SECONDS = 1.0
DEFAULT_RETRY_MAX_WAIT_SECONDS = 1800.0  # 30 minutes
CONTROL_LIMITS_CACHE_TTL_SECONDS = 86_400  # 24 hours
EDGE_EVAL_LATENCY_BUDGET_MS = 100

# API
API_PREFIX = "/api/v1"
HL7_MLLP_DEFAULT_PORT = 2575
