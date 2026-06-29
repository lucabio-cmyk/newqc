"""Fixtures for the cross-project integration suite.

Wires the cloud and edge repositories onto ``sys.path`` and exposes:

* ``cloud_app``    - the real qc_evaluation FastAPI app object.
* ``cloud_client`` - an in-process Starlette/FastAPI ``TestClient`` over the app.
* ``edge_cache``   - an :class:`EdgeCache` backed by a tmp-path SQLite file.

The cloud app degrades gracefully without a DB or the ML-inference service, so
every fixture works fully offline and in-process. The factories under
``shared.testing`` supply the deterministic test data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Path wiring: make ``cloud.*`` and ``edge.*`` importable.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CLOUD_ROOT = _REPO_ROOT / "qconnect-ai-cloud"
_EDGE_ROOT = _REPO_ROOT / "qconnect-ai-edge"

for _p in (_CLOUD_ROOT, _EDGE_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture()
def cloud_app():
    """Return the real qc_evaluation FastAPI app (skips if deps missing)."""
    pytest.importorskip("fastapi")
    from cloud.services.qc_evaluation.main import app  # noqa: WPS433

    return app


@pytest.fixture()
def cloud_client(cloud_app):
    """An in-process TestClient bound to the cloud app."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    return fastapi_testclient.TestClient(cloud_app)


@pytest.fixture()
def edge_cache(tmp_path):
    """An :class:`EdgeCache` on a tmp SQLite file (closed on teardown)."""
    from edge.qc_inference.cache.sqlite_manager import EdgeCache  # noqa: WPS433

    cache = EdgeCache(str(tmp_path / "edge_cache.db"))
    try:
        yield cache
    finally:
        cache.close()
