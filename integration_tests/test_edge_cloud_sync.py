"""Headline integration test: an edge node syncing to the in-process cloud.

The :class:`CloudUploader` is driven against the REAL cloud FastAPI app through
an httpx ``ASGITransport`` (no sockets, no running server), injected via the
uploader's ``client_factory`` seam. This exercises the full edge -> cloud path:
build pending rows in the edge SQLite cache -> upload batch -> cloud validates &
re-evaluates each record -> rows marked uploaded on 2xx.

pytest-asyncio is intentionally NOT required: the async uploader coroutines are
driven with ``asyncio.run`` inside plain sync test functions so the suite runs
anywhere.
"""

from __future__ import annotations

import asyncio

import httpx

from edge.data_sync.uploader import CloudUploader
from edge.qc_inference.hl7.parser import HL7Parser
from edge.qc_inference.models.qconnect import QConnectEngine
from edge.qc_inference.models.westgard import WestgardEngine
from shared.models import AnalyteType, QCDataInput, QCLevelType
from shared.testing import make_control_limits, make_hl7_message, make_qc_history, make_qc_input

BASE_URL = "http://cloud"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _save_pending(cache, qc: QCDataInput, result: dict) -> int:
    """Persist a QC result as pending, embedding the full QC input.

    The QC input is stored under ``evaluation_result["qc_input"]`` so the
    uploader's ``_build_batch_payload`` can recover the full ``QCDataInput`` the
    cloud batch endpoint requires.
    """
    qc_data = qc.model_dump(mode="json")
    enriched = dict(result)
    enriched["qc_input"] = qc_data
    return cache.save_qc_result(qc_data, enriched)


def _asgi_factory(cloud_app):
    """Return a ``client_factory`` producing an ASGI-backed httpx client."""

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cloud_app),
            base_url=BASE_URL,
        )

    return factory


# --------------------------------------------------------------------------- #
# Headline sync test
# --------------------------------------------------------------------------- #
def test_edge_syncs_pending_to_cloud(cloud_app, edge_cache) -> None:
    """Pending edge rows upload to the in-process cloud and are marked done."""
    # Seed several pending QC results.
    for i in range(7):
        qc = make_qc_input(operator_id=f"EMP{i:05d}", result_value=1.45 + i * 0.001)
        _save_pending(edge_cache, qc, {"qc_status": "PASS", "severity": "LOW"})
    assert edge_cache.count_pending_uploads() == 7

    uploader = CloudUploader(
        cloud_url=BASE_URL,
        lab_id="lab-genova-001",
        auth_token="test-jwt",
        cache=edge_cache,
        client_factory=_asgi_factory(cloud_app),
    )

    metrics = asyncio.run(uploader.sync_pending_uploads())

    assert metrics["offline"] is False
    assert metrics["uploaded"] == 7
    assert metrics["batches"] == 1
    # All rows acknowledged -> nothing left pending.
    assert edge_cache.count_pending_uploads() == 0
    assert edge_cache.get_pending_uploads() == []
    assert uploader.last_sync is not None


def test_cloud_accepts_uploaded_batch(cloud_app, cloud_client, edge_cache) -> None:
    """The cloud counts the synced records as accepted (verified via a spy)."""
    for i in range(3):
        qc = make_qc_input(operator_id=f"EMP{i:05d}")
        _save_pending(edge_cache, qc, {"qc_status": "PASS"})

    accepted_counts: list[int] = []

    class _SpyClient(httpx.AsyncClient):
        async def post(self, *args, **kwargs):  # type: ignore[override]
            resp = await super().post(*args, **kwargs)
            try:
                accepted_counts.append(resp.json().get("accepted"))
            except Exception:  # pragma: no cover - spy best-effort
                pass
            return resp

    def factory() -> httpx.AsyncClient:
        return _SpyClient(transport=httpx.ASGITransport(app=cloud_app), base_url=BASE_URL)

    uploader = CloudUploader(
        cloud_url=BASE_URL,
        lab_id="lab-genova-001",
        auth_token="test-jwt",
        cache=edge_cache,
        client_factory=factory,
    )
    metrics = asyncio.run(uploader.sync_pending_uploads())

    assert metrics["uploaded"] == 3
    assert accepted_counts == [3]  # the cloud accepted all 3 records
    assert edge_cache.count_pending_uploads() == 0


# --------------------------------------------------------------------------- #
# HL7 -> parse -> edge-evaluate -> cache chain
# --------------------------------------------------------------------------- #
def test_hl7_parse_evaluate_cache_chain(edge_cache) -> None:
    """make_hl7_message -> parse -> build input -> evaluate -> cache as pending."""
    raw = make_hl7_message(analyte="HCV-AB", value=1.45, control_id="MSG12345")
    parsed = HL7Parser().parse_message(raw)
    assert parsed["message_control_id"] == "MSG12345"
    assert parsed["analyte_code"] == "HCV-AB"
    assert parsed["result_value"] == 1.45

    # Build a QCDataInput from the parsed observation.
    qc = QCDataInput(
        lab_id="lab-genova-001",
        analyzer_id=parsed["sender"] or "ARCHITECT",
        analyte_code=parsed["analyte_code"],
        analyte_type=AnalyteType.SEROLOGY,
        qc_lot_id="QC-HCV-DIAMEX-202603-001",
        qc_level=QCLevelType.NORMAL,
        result_value=parsed["result_value"],
        target_value=1.50,
        sd_value=0.08,
        operator_id="EMP00234",
    )

    # Build limits/history and run the edge engines.
    history = make_qc_history(mean=1.50, sd=0.08, n=30, seed=42)
    limits = make_control_limits(history)
    westgard = WestgardEngine().evaluate(qc.result_value, qc.target_value, qc.sd_value, history)
    qconnect = QConnectEngine().evaluate(qc.result_value, limits)
    assert westgard["status"] in {"PASS", "FAIL", "REVIEW_REQUIRED"}
    assert qconnect["status"] in {"PASS", "FAIL", "REVIEW_REQUIRED"}

    result = {
        "qc_status": westgard["status"],
        "westgard": westgard,
        "qconnect": qconnect,
    }
    row_id = _save_pending(edge_cache, qc, result)
    assert row_id > 0

    pending = edge_cache.get_pending_uploads()
    assert len(pending) == 1
    assert pending[0]["analyte_code"] == "HCV-AB"
    assert pending[0]["evaluation_result"]["qc_status"] == westgard["status"]


# --------------------------------------------------------------------------- #
# Offline degradation
# --------------------------------------------------------------------------- #
def test_sync_offline_is_safe_and_keeps_rows(edge_cache) -> None:
    """When the cloud is unreachable, sync must not raise and rows must remain."""
    for i in range(4):
        _save_pending(edge_cache, make_qc_input(operator_id=f"EMP{i:05d}"), {"qc_status": "PASS"})
    assert edge_cache.count_pending_uploads() == 4

    class _DeadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):  # type: ignore[override]
            raise httpx.ConnectError("simulated offline", request=request)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=_DeadTransport(), base_url=BASE_URL)

    uploader = CloudUploader(
        cloud_url=BASE_URL,
        lab_id="lab-genova-001",
        auth_token="test-jwt",
        cache=edge_cache,
        client_factory=factory,
        max_attempts=1,  # avoid real backoff sleeps while still exercising retry path
    )

    # Must not raise even though every request errors.
    metrics = asyncio.run(uploader.sync_pending_uploads())

    assert metrics["offline"] is True
    assert metrics["uploaded"] == 0
    # Offline-safe: nothing was lost.
    assert edge_cache.count_pending_uploads() == 4
