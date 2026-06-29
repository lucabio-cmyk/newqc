"""Entry point for the ``data-sync`` daemon container.

Wires a :class:`CloudUploader` to the shared :class:`EdgeCache` using environment
configuration and runs the sync loop under ``asyncio.run``. Run with::

    python -m data_sync.run

All configuration is read from the environment so the same image works across
labs without rebuilds.
"""

from __future__ import annotations

import asyncio
import os
import sys

from loguru import logger

from edge.data_sync.uploader import CloudUploader, DEFAULT_SYNC_INTERVAL
from edge.qc_inference.cache.sqlite_manager import EdgeCache


def _bool_env(name: str, default: bool = True) -> bool:
    """Parse a boolean environment variable."""
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


async def _main() -> None:
    """Configure and run the sync loop until interrupted."""
    lab_id = os.getenv("LAB_ID", "lab-unknown")
    cloud_url = os.getenv("CLOUD_URL", "").rstrip("/")
    auth_token = os.getenv("LAB_AUTH_TOKEN", "")
    db_path = os.getenv("DB_PATH", "/data/qc_cache.db")
    interval = int(os.getenv("SYNC_INTERVAL", str(DEFAULT_SYNC_INTERVAL)))
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    # TLS: True (verify) by default; point at a CA bundle for mTLS if provided.
    ca_bundle = os.getenv("CLOUD_CA_BUNDLE")
    verify: bool | str = ca_bundle if ca_bundle else _bool_env("CLOUD_TLS_VERIFY", True)

    logger.remove()
    logger.add(sys.stderr, level=log_level)
    logger.info("data-sync starting: lab={} cloud={} interval={}s", lab_id, cloud_url, interval)

    with EdgeCache(db_path) as cache:
        uploader = CloudUploader(
            cloud_url=cloud_url,
            lab_id=lab_id,
            auth_token=auth_token,
            cache=cache,
            verify=verify,
        )
        try:
            await uploader.sync_loop(interval_seconds=interval)
        except asyncio.CancelledError:  # pragma: no cover
            logger.info("data-sync cancelled; shutting down")


def main() -> None:
    """Synchronous wrapper invoked by the container CMD."""
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("data-sync interrupted; exiting")


if __name__ == "__main__":
    main()
