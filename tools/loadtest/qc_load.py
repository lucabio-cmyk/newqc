#!/usr/bin/env python3
"""QConnect-AI smoke & load test harness.

A dependency-light (httpx + stdlib) client that drives a *running* QConnect-AI
cloud stack over HTTP. Two subcommands:

* ``smoke`` — fast end-to-end sanity check: /health, a few /qc/evaluate calls
  (asserting the response contract), a batch ingest and /metrics. Exits non-zero
  on the first failure, so it is suitable as a CI gate after ``docker compose up``.
* ``load``  — closed-loop load generator: N concurrent workers fire evaluate
  requests for a fixed duration and report throughput, latency percentiles and
  the error rate.

Examples
--------
    python qc_load.py smoke --base-url http://localhost:8000
    python qc_load.py load  --base-url http://localhost:8000 --concurrency 20 --duration 30

The payload builder and the latency summariser are pure functions (no network),
exercised by ``test_loadtest.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from dataclasses import dataclass, field

try:  # httpx is only needed to actually hit a server, not to import this module.
    import httpx
except Exception:  # pragma: no cover - import guard
    httpx = None  # type: ignore

API_PREFIX = "/api/v1"
_ANALYTES = [
    ("HCV-AB", "serology"),
    ("HIV-AB", "serology"),
    ("TROPONIN-I", "chemistry"),
    ("GLUCOSE", "chemistry"),
]


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def build_payload(seed: int, index: int) -> dict:
    """Build a deterministic QCDataInput-shaped payload for a given (seed, index).

    Most points sit near the target; ~1 in 11 is pushed beyond 3 SD so the load
    mix exercises the FAIL path too.
    """
    rng = random.Random((seed * 1_000_003) ^ index)
    analyte, atype = _ANALYTES[index % len(_ANALYTES)]
    target = 1.50
    sd = 0.08
    # Mostly in-control; occasionally a gross outlier.
    deviation = rng.gauss(0, 1) if index % 11 else rng.choice([4.0, -4.0])
    value = round(max(0.01, target + deviation * sd), 4)
    return {
        "lab_id": f"lab-load-{seed:03d}",
        "analyzer_id": f"AZ-{index % 5}",
        "analyte_code": analyte,
        "analyte_type": atype,
        "qc_lot_id": f"LOT-{seed}-{index % 7}",
        "qc_level": "NORMAL",
        "result_value": value,
        "target_value": target,
        "sd_value": sd,
        "operator_id": f"OP-{index % 3}",
    }


def percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile over a pre-sorted list (pct in [0,100])."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


@dataclass
class LoadResult:
    """Aggregated outcome of a load run."""

    total: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    duration_s: float = 0.0

    def summary(self) -> dict:
        lat = sorted(self.latencies_ms)
        ok = self.total - self.errors
        rps = ok / self.duration_s if self.duration_s > 0 else 0.0
        return {
            "requests": self.total,
            "ok": ok,
            "errors": self.errors,
            "error_rate": round(self.errors / self.total, 4) if self.total else 0.0,
            "throughput_rps": round(rps, 1),
            "latency_ms_p50": round(percentile(lat, 50), 2),
            "latency_ms_p95": round(percentile(lat, 95), 2),
            "latency_ms_p99": round(percentile(lat, 99), 2),
            "latency_ms_max": round(lat[-1], 2) if lat else 0.0,
        }


# --------------------------------------------------------------------------- #
# Smoke
# --------------------------------------------------------------------------- #
async def run_smoke(base_url: str, timeout: float) -> int:
    """Return 0 on success, 1 on the first failed assertion."""
    assert httpx is not None, "httpx is required to run the harness"
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        status = "ok " if cond else "FAIL"
        print(f"  [{status}] {msg}")
        if not cond:
            failures.append(msg)

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as c:
        try:
            r = await c.get("/health")
            check(r.status_code == 200, f"GET /health -> {r.status_code}")
            check(r.json().get("service") == "qc-evaluation", "health.service == qc-evaluation")
        except Exception as exc:  # pragma: no cover - network
            check(False, f"GET /health raised {exc!r}")
            return 1

        # A clean in-range evaluation.
        r = await c.post(f"{API_PREFIX}/qc/evaluate", json=build_payload(1, 0))
        check(r.status_code == 200, f"POST /qc/evaluate -> {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            check(
                body["qc_status"] in {"PASS", "FAIL", "REVIEW_REQUIRED", "HOLD_PENDING_AI"},
                f"evaluate qc_status valid ({body.get('qc_status')})",
            )
            for k in ("westgard", "qconnect", "sigma"):
                check(k in body.get("legacy_results", {}), f"legacy_results has '{k}'")

        # An out-of-control value should FAIL.
        r = await c.post(f"{API_PREFIX}/qc/evaluate", json=build_payload(1, 10))
        check(r.status_code == 200 and r.json()["qc_status"] == "FAIL", "outlier -> FAIL")

        # Batch ingest.
        recs = [build_payload(2, i) for i in range(5)]
        r = await c.post(
            f"{API_PREFIX}/labs/lab-load-002/qc/batch",
            json={"lab_id": "lab-load-002", "records": recs},
        )
        check(r.status_code == 200 and r.json().get("accepted") == 5, "batch accepted == 5")

        # Metrics.
        r = await c.get("/metrics")
        check(r.status_code == 200, f"GET /metrics -> {r.status_code}")
        check("qconnect_qc_evaluations_total" in r.text, "/metrics exposes evaluations counter")

    print()
    if failures:
        print(f"SMOKE FAILED: {len(failures)} check(s) failed")
        return 1
    print("SMOKE PASSED")
    return 0


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
async def _worker(
    client: "httpx.AsyncClient",
    deadline: float,
    seed: int,
    result: LoadResult,
) -> None:
    i = 0
    while time.monotonic() < deadline:
        payload = build_payload(seed, i)
        i += 1
        start = time.monotonic()
        try:
            r = await client.post(f"{API_PREFIX}/qc/evaluate", json=payload)
            ok = r.status_code == 200
        except Exception:
            ok = False
        result.latencies_ms.append((time.monotonic() - start) * 1000.0)
        result.total += 1
        if not ok:
            result.errors += 1


async def run_load(base_url: str, concurrency: int, duration: float, timeout: float) -> int:
    assert httpx is not None, "httpx is required to run the harness"
    result = LoadResult()
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    print(f"load: {concurrency} workers x {duration}s against {base_url}")
    start = time.monotonic()
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout, limits=limits) as c:
        deadline = time.monotonic() + duration
        await asyncio.gather(*(_worker(c, deadline, w, result) for w in range(concurrency)))
    result.duration_s = time.monotonic() - start

    summary = result.summary()
    print("\n=== load summary ===")
    for k, v in summary.items():
        print(f"  {k:18}: {v}")
    # Non-zero exit if the run was unhealthy.
    return 1 if summary["error_rate"] > 0.01 else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="QConnect-AI smoke & load harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("smoke", help="end-to-end sanity check")
    s.add_argument("--base-url", default="http://localhost:8000")
    s.add_argument("--timeout", type=float, default=10.0)

    load = sub.add_parser("load", help="closed-loop load generator")
    load.add_argument("--base-url", default="http://localhost:8000")
    load.add_argument("--concurrency", type=int, default=10)
    load.add_argument("--duration", type=float, default=15.0)
    load.add_argument("--timeout", type=float, default=10.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if httpx is None:
        print("error: httpx is required (pip install httpx)", file=sys.stderr)
        return 2
    if args.cmd == "smoke":
        return asyncio.run(run_smoke(args.base_url, args.timeout))
    if args.cmd == "load":
        return asyncio.run(run_load(args.base_url, args.concurrency, args.duration, args.timeout))
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
