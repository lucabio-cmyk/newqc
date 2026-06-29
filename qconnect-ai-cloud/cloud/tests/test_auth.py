"""Auth / RBAC enforcement tests for the qc_evaluation service.

These flip ``settings.require_auth`` to True (it defaults False for dev/test) and
verify the hierarchical role checks on the protected endpoints. JWTs are minted
with the same secret the service decodes with.
"""

from __future__ import annotations

import pytest

API_PREFIX = "/api/v1"


@pytest.fixture()
def auth_settings():
    """Enable auth on the shared settings singleton, restoring it afterwards."""
    from cloud.config.settings import get_settings

    settings = get_settings()
    previous = settings.require_auth
    settings.require_auth = True
    try:
        yield settings
    finally:
        settings.require_auth = previous


def _token(settings, role: str) -> str:
    """Mint a signed JWT carrying ``role`` for the configured secret."""
    from cloud.config.security import create_jwt

    return create_jwt(
        "user-1",
        settings.jwt_secret,
        extra_claims={"role": role},
        algorithm=settings.jwt_algorithm,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_dev_mode_bypasses_auth(client, sample_qc_data) -> None:
    """With require_auth False (default), no token is needed."""
    resp = client.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    assert resp.status_code == 200


def test_evaluate_requires_token_when_enabled(client, auth_settings, sample_qc_data) -> None:
    """No Authorization header -> 401 when auth is required."""
    resp = client.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    assert resp.status_code == 401


def test_evaluate_forbidden_for_insufficient_role(client, auth_settings, sample_qc_data) -> None:
    """A 'viewer' cannot evaluate (needs 'operator') -> 403."""
    headers = _bearer(_token(auth_settings, "viewer"))
    resp = client.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data, headers=headers)
    assert resp.status_code == 403


def test_evaluate_allowed_for_operator(client, auth_settings, sample_qc_data) -> None:
    """An 'operator' token is accepted on the evaluate endpoint."""
    headers = _bearer(_token(auth_settings, "operator"))
    resp = client.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data, headers=headers)
    assert resp.status_code == 200


def test_higher_role_satisfies_lower_requirement(client, auth_settings, sample_qc_data) -> None:
    """RBAC is hierarchical: a lab_director (rank 40) satisfies operator (20)."""
    headers = _bearer(_token(auth_settings, "lab_director"))
    resp = client.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data, headers=headers)
    assert resp.status_code == 200


def test_lab_status_allows_viewer(client, auth_settings) -> None:
    """The read-only status endpoint requires only the 'viewer' role."""
    headers = _bearer(_token(auth_settings, "viewer"))
    resp = client.get(f"{API_PREFIX}/labs/lab-genova-001/qc/status", headers=headers)
    assert resp.status_code == 200


def test_invalid_token_rejected(client, auth_settings, sample_qc_data) -> None:
    """A malformed/garbage bearer token -> 401."""
    resp = client.post(
        f"{API_PREFIX}/qc/evaluate",
        json=sample_qc_data,
        headers=_bearer("not-a-real-jwt"),
    )
    assert resp.status_code == 401


def test_health_and_metrics_are_public(client, auth_settings) -> None:
    """Ops endpoints stay open even when auth is enabled."""
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200
