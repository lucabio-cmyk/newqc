"""Optional Locust scenario for QConnect-AI (richer load profiles + web UI).

Install locust separately (it is NOT a project dependency):

    pip install locust
    locust -f tools/loadtest/locustfile.py --host http://localhost:8000

The dependency-free ``qc_load.py load`` covers CI / headless use; this file is
for interactive exploration (ramping, charts) when locust is available.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from qc_load import API_PREFIX, build_payload  # noqa: E402

try:
    from locust import HttpUser, between, task
except Exception as exc:  # pragma: no cover - locust is optional
    raise SystemExit("locust is not installed. Run: pip install locust") from exc


class QCUser(HttpUser):
    """Simulates an analyzer feed posting QC results."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        self._i = 0

    @task(10)
    def evaluate(self) -> None:
        self._i += 1
        self.client.post(f"{API_PREFIX}/qc/evaluate", json=build_payload(1, self._i))

    @task(1)
    def health(self) -> None:
        self.client.get("/health")
