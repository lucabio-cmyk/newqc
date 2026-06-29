#!/usr/bin/env python3
"""Seed a few reference rows (qc_materials, control_limits).

Best-effort: if no database is reachable (or the async driver is missing) the
script logs a warning and exits 0 so it is safe to run in CI bootstrap steps.

Usage:
    DATABASE_URL=postgresql://qconnect:qconnect@localhost:5432/qconnect \\
        python scripts/seed_data.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# Sample QC material lots.
QC_MATERIALS: list[dict] = [
    {
        "vendor_name": "DiaMex",
        "product_code": "HCV-QC",
        "lot_number": "QC-HCV-DIAMEX-202603-001",
        "analyte_code": "HCV-AB",
        "qc_level": "NORMAL",
        "target_value": 1.50,
        "target_sd": 0.08,
        "matrix_type": "human_serum",
        "negative_hiv": True,
        "negative_hcv": False,
        "negative_syphilis": True,
        "commutability_tested": True,
        "status": "ACTIVE",
    },
    {
        "vendor_name": "BioRad",
        "product_code": "TNI-QC",
        "lot_number": "QC-TNI-BIORAD-202603-002",
        "analyte_code": "TROPONIN-I",
        "qc_level": "NORMAL",
        "target_value": 0.040,
        "target_sd": 0.004,
        "matrix_type": "human_serum",
        "negative_hiv": True,
        "negative_hcv": True,
        "negative_syphilis": True,
        "commutability_tested": True,
        "status": "ACTIVE",
    },
]

# Sample control limits.
CONTROL_LIMITS: list[dict] = [
    {
        "analyte_code": "HCV-AB",
        "assay_product_code": "ARCHITECT-HCV",
        "qc_lot_id": "QC-HCV-DIAMEX-202603-001",
        "qc_level": "NORMAL",
        "westgard_mean": 1.50,
        "westgard_sd": 0.08,
        "westgard_limit_3s_lower": 1.26,
        "westgard_limit_3s_upper": 1.74,
        "qconnect_percentile_5": 1.36,
        "qconnect_percentile_95": 1.66,
        "qconnect_lcl": 1.30,
        "qconnect_ucl": 1.70,
        "sigma_metric": 5.0,
        "distribution_type": "skewed",
        "shapiro_wilk_p": 0.012,
    },
    {
        "analyte_code": "TROPONIN-I",
        "assay_product_code": "ARCHITECT-TNI",
        "qc_lot_id": "QC-TNI-BIORAD-202603-002",
        "qc_level": "NORMAL",
        "westgard_mean": 0.040,
        "westgard_sd": 0.004,
        "westgard_limit_3s_lower": 0.028,
        "westgard_limit_3s_upper": 0.052,
        "qconnect_percentile_5": 0.033,
        "qconnect_percentile_95": 0.047,
        "qconnect_lcl": 0.031,
        "qconnect_ucl": 0.049,
        "sigma_metric": 4.2,
        "distribution_type": "gaussian",
        "shapiro_wilk_p": 0.42,
    },
]


def _normalize_dsn(dsn: str) -> str:
    """Strip the SQLAlchemy async driver suffix so asyncpg can connect."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://").replace("+asyncpg", "")


async def _seed() -> int:
    dsn = os.getenv(
        "DATABASE_URL",
        "postgresql://qconnect:qconnect@localhost:5432/qconnect",
    )
    dsn = _normalize_dsn(dsn)

    try:
        import asyncpg  # type: ignore
    except Exception as exc:  # pragma: no cover
        print(f"[seed] asyncpg not installed ({exc}); skipping.", file=sys.stderr)
        return 0

    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:
        print(f"[seed] could not connect to DB ({exc}); skipping.", file=sys.stderr)
        return 0

    try:
        for mat in QC_MATERIALS:
            cols = ", ".join(mat.keys())
            placeholders = ", ".join(f"${i + 1}" for i in range(len(mat)))
            await conn.execute(
                f"INSERT INTO qc_materials ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT (lot_number) DO NOTHING",
                *mat.values(),
            )
        for lim in CONTROL_LIMITS:
            cols = ", ".join(lim.keys())
            placeholders = ", ".join(f"${i + 1}" for i in range(len(lim)))
            await conn.execute(
                f"INSERT INTO control_limits ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT (analyte_code, assay_product_code, qc_lot_id, qc_level) "
                f"DO NOTHING",
                *lim.values(),
            )
        print(
            f"[seed] inserted up to {len(QC_MATERIALS)} qc_materials and "
            f"{len(CONTROL_LIMITS)} control_limits rows."
        )
    finally:
        await conn.close()
    return 0


def main() -> int:
    """Entry point."""
    try:
        return asyncio.run(_seed())
    except Exception as exc:  # pragma: no cover - never hard-fail bootstrap
        print(f"[seed] unexpected error ({exc}); skipping.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
