"""Tests for the federated-learning service: API flow + unit tests."""

from __future__ import annotations

import math

from fastapi.testclient import TestClient

from cloud.services.federated_learning.aggregation import dp, fedavg
from cloud.services.federated_learning.main import app


def _client() -> TestClient:
    return TestClient(app)


# -- API flow -----------------------------------------------------------------
def test_full_round_flow_weighted_average() -> None:
    with _client() as client:
        rn = client.post("/rounds/start").json()["round_number"]

        labs = [
            {"lab_id": "A", "weights": [1.0, 2.0], "num_samples": 10},
            {"lab_id": "B", "weights": [3.0, 4.0], "num_samples": 20},
            {"lab_id": "C", "weights": [5.0, 6.0], "num_samples": 70},
        ]
        for i, lab in enumerate(labs, start=1):
            resp = client.post(f"/rounds/{rn}/submit", json=lab)
            assert resp.status_code == 200, resp.text
            assert resp.json()["accepted"] is True
            assert resp.json()["total_updates"] == i

        # Aggregate WITHOUT noise (clip large enough to not clip) to check FedAvg.
        # epsilon=inf disables noise.
        resp = client.post(
            f"/rounds/{rn}/aggregate",
            json={"epsilon": None, "delta": 1e-5, "clip_norm": 1000.0},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["participating_labs"] == 3
        assert body["global_model_version"] == 1
        assert body["dp_sigma"] == 0.0
        # Expected weighted average computed by hand: [4.2, 5.2].
        preview = body["global_weights_preview"]
        assert math.isclose(preview[0], 4.2, rel_tol=1e-9)
        assert math.isclose(preview[1], 5.2, rel_tol=1e-9)


def test_aggregate_with_finite_epsilon_has_positive_sigma() -> None:
    with _client() as client:
        rn = client.post("/rounds/start").json()["round_number"]
        client.post(
            f"/rounds/{rn}/submit",
            json={"lab_id": "A", "weights": [1.0, 2.0], "num_samples": 5},
        )
        resp = client.post(
            f"/rounds/{rn}/aggregate",
            json={"epsilon": 1.0, "delta": 1e-5, "clip_norm": 1.0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dp_sigma"] > 0


def test_mismatched_vector_length_rejected() -> None:
    with _client() as client:
        rn = client.post("/rounds/start").json()["round_number"]
        client.post(
            f"/rounds/{rn}/submit",
            json={"lab_id": "A", "weights": [1.0, 2.0], "num_samples": 5},
        )
        # Aggregate to fix the global model dimension at 2.
        client.post(
            f"/rounds/{rn}/aggregate",
            json={"epsilon": None, "delta": 1e-5, "clip_norm": 1000.0},
        )
        rn2 = client.post("/rounds/start").json()["round_number"]
        resp = client.post(
            f"/rounds/{rn2}/submit",
            json={"lab_id": "B", "weights": [1.0, 2.0, 3.0], "num_samples": 5},
        )
        assert 400 <= resp.status_code < 500


def test_aggregate_empty_round_rejected() -> None:
    with _client() as client:
        rn = client.post("/rounds/start").json()["round_number"]
        resp = client.post(
            f"/rounds/{rn}/aggregate",
            json={"epsilon": 1.0, "delta": 1e-5, "clip_norm": 1.0},
        )
        assert 400 <= resp.status_code < 500


def test_model_current_metadata() -> None:
    with _client() as client:
        before = client.get("/model/current").json()
        assert before["initialised"] is False
        assert before["global_model_version"] == 0

        rn = client.post("/rounds/start").json()["round_number"]
        client.post(
            f"/rounds/{rn}/submit",
            json={"lab_id": "A", "weights": [1.0, 2.0, 3.0], "num_samples": 5},
        )
        client.post(
            f"/rounds/{rn}/aggregate",
            json={"epsilon": None, "delta": 1e-5, "clip_norm": 1000.0},
        )
        after = client.get("/model/current").json()
        assert after["initialised"] is True
        assert after["dim"] == 3
        assert after["global_model_version"] == 1
        assert after["updated_at"] is not None


def test_health() -> None:
    with _client() as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "federated-learning"


def test_metrics() -> None:
    with _client() as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "qconnect_fl_rounds_total" in resp.text


# -- Unit tests ---------------------------------------------------------------
def test_federated_average_unit() -> None:
    updates = [
        {"weights": [0.0, 0.0], "num_samples": 1},
        {"weights": [2.0, 4.0], "num_samples": 3},
    ]
    # (1*[0,0] + 3*[2,4]) / 4 = [6/4, 12/4] = [1.5, 3.0]
    avg = fedavg.federated_average(updates)
    assert math.isclose(avg[0], 1.5)
    assert math.isclose(avg[1], 3.0)


def test_federated_average_length_mismatch() -> None:
    import pytest

    with pytest.raises(ValueError):
        fedavg.federated_average(
            [
                {"weights": [1.0, 2.0], "num_samples": 1},
                {"weights": [1.0], "num_samples": 1},
            ]
        )


def test_apply_update_and_delta_norm() -> None:
    new = fedavg.apply_update([1.0, 1.0], [2.0, 2.0], lr=0.5)
    assert new == [2.0, 2.0]
    assert math.isclose(fedavg.model_delta_norm([3.0, 4.0], [0.0, 0.0]), 5.0)


def test_clip_l2() -> None:
    # Norm of [3,4] is 5; clip to 1 -> scale 0.2 -> [0.6, 0.8].
    clipped = dp.clip_l2([3.0, 4.0], 1.0)
    assert math.isclose(clipped[0], 0.6)
    assert math.isclose(clipped[1], 0.8)
    # Already within bound -> unchanged.
    assert dp.clip_l2([0.1, 0.1], 10.0) == [0.1, 0.1]


def test_add_gaussian_noise_determinism() -> None:
    a = dp.add_gaussian_noise([0.0, 0.0, 0.0], sigma=1.0, seed=42)
    b = dp.add_gaussian_noise([0.0, 0.0, 0.0], sigma=1.0, seed=42)
    c = dp.add_gaussian_noise([0.0, 0.0, 0.0], sigma=1.0, seed=7)
    assert a == b
    assert a != c
    # sigma=0 returns input unchanged.
    assert dp.add_gaussian_noise([1.0, 2.0], sigma=0.0, seed=1) == [1.0, 2.0]


def test_gaussian_sigma_for() -> None:
    sigma = dp.gaussian_sigma_for(epsilon=1.0, delta=1e-5, sensitivity=1.0)
    expected = math.sqrt(2.0 * math.log(1.25 / 1e-5)) / 1.0
    assert math.isclose(sigma, expected)
    # Non-finite epsilon -> no noise.
    assert dp.gaussian_sigma_for(float("inf"), 1e-5, 1.0) == 0.0
