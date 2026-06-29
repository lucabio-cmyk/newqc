"""Pytest fixtures for the QConnect-AI cloud test suite.

Ensures the qc_evaluation service directory and the repo root are importable, and
provides shared fixtures: a sample QCDataInput dict and a Starlette/FastAPI
``TestClient`` bound to the qc_evaluation app.

All fixtures are designed to work WITHOUT a database or network — the ML call in
the service is best-effort and degrades when unreachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Path wiring
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]  # …/qconnect-ai-cloud
_SERVICE_DIR = _REPO_ROOT / "cloud" / "services" / "qc_evaluation"

for p in (_REPO_ROOT, _SERVICE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture()
def sample_qc_data() -> dict:
    """A valid QCDataInput payload (HCV serology, normal level)."""
    return {
        "lab_id": "lab-genova-001",
        "analyzer_id": "ABBOTT-ARCHITECT-001",
        "analyte_code": "HCV-AB",
        "analyte_type": "serology",
        "qc_lot_id": "QC-HCV-DIAMEX-202603-001",
        "qc_level": "NORMAL",
        "result_value": 1.45,
        "target_value": 1.50,
        "sd_value": 0.08,
        "operator_id": "EMP00234",
        "timestamp": "2026-03-15T09:30:00Z",
    }


@pytest.fixture()
def failing_qc_data() -> dict:
    """A QCDataInput that trips the Westgard 1-3S rule (> +3 SD)."""
    return {
        "lab_id": "lab-genova-001",
        "analyzer_id": "ABBOTT-ARCHITECT-001",
        "analyte_code": "TROPONIN-I",
        "analyte_type": "chemistry",
        "qc_lot_id": "QC-TNI-202603-001",
        "qc_level": "NORMAL",
        "result_value": 0.053,
        "target_value": 0.040,
        "sd_value": 0.004,
        "operator_id": "EMP00234",
        "timestamp": "2026-03-15T09:30:00Z",
    }


@pytest.fixture()
def client():
    """Return a TestClient for the qc_evaluation FastAPI app (no DB).

    Explicitly overrides the app's ``get_db`` dependency to yield ``None`` so the
    deterministic no-DB code path is taken regardless of whether asyncpg /
    Postgres exist in the environment. This keeps the offline tests fast (no
    multi-second DB connection attempts) and DB-free. The override is cleared on
    teardown.

    Skips the whole test module if FastAPI / its test deps are not installed.
    """
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    # Import the app only after path wiring above is in place.
    from cloud.services.qc_evaluation import main as qc_main  # noqa: WPS433

    # Override the exact get_db object the routes reference. main.py may import
    # get_db via the container path ("dependencies") rather than the fully
    # qualified package path, so use qc_main.get_db (not a fresh import) as the
    # override key — otherwise the override silently fails to bind.
    get_db = qc_main.get_db

    async def _no_db():
        yield None

    qc_main.app.dependency_overrides[get_db] = _no_db
    try:
        yield fastapi_testclient.TestClient(qc_main.app)
    finally:
        qc_main.app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def client_with_db():
    """Return a TestClient backed by an in-memory aiosqlite database.

    Event-loop sharing is the crux: Starlette's ``TestClient`` drives the ASGI
    app through a single *blocking portal* (one dedicated event loop running in a
    background thread for the life of the client). aiosqlite binds its DBAPI
    connection to the loop that first opened it, so the in-memory database is
    only usable from that one loop. We therefore run schema setup AND every
    request's session on the portal's loop via ``client.portal.call(...)``.

    A single connection is pinned with :class:`~sqlalchemy.pool.StaticPool` (an
    in-memory SQLite DB lives only as long as its connection — StaticPool keeps
    exactly one, so rows persist across requests). Tests inspect rows with the
    exposed ``run_db`` helper, which also runs on the portal loop.
    """
    pytest.importorskip("aiosqlite")
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from cloud.db.models import Base
    from cloud.services.qc_evaluation import main as qc_main  # noqa: WPS433

    # Use the exact get_db object the routes reference (see the ``client``
    # fixture note) so the override actually binds.
    get_db = qc_main.get_db

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _db():
        session = sessionmaker()
        try:
            yield session
        finally:
            await session.close()

    qc_main.app.dependency_overrides[get_db] = _db

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _drop() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    # Entering the TestClient context starts its blocking portal/event loop.
    with fastapi_testclient.TestClient(qc_main.app) as client:
        client.portal.call(_create)

        def run_db(coro_factory):
            """Run ``coro_factory(session)`` on the portal loop with a session.

            ``coro_factory`` is an async function taking an ``AsyncSession`` and
            returning a value; it executes on the same loop as the requests so it
            sees committed rows in the shared in-memory DB.
            """

            async def _runner():
                async with sessionmaker() as session:
                    return await coro_factory(session)

            return client.portal.call(_runner)

        client.run_db = run_db  # type: ignore[attr-defined]
        try:
            yield client
        finally:
            client.portal.call(_drop)
            qc_main.app.dependency_overrides.pop(get_db, None)
