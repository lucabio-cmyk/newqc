"""Cloud uploader: drains the edge cache and pulls model/limit updates.

This is the heart of the ``data-sync`` service. It is built around three ideas:

* **Offline-safe**: pending QC results stay in the cache until the cloud
  acknowledges them (HTTP 2xx). A failed/absent upload simply leaves them queued.
* **Batched**: results are uploaded in batches of <=100 to bound request size.
* **Resilient**: network calls back off exponentially and never raise out of the
  loop, so a transient outage cannot crash the daemon.

Security note: requests authenticate with a per-lab JWT via the
``Authorization: Bearer`` header. ``httpx``'s ``verify`` parameter is left at its
secure default (TLS verification on); set a custom CA bundle for mTLS by passing
``verify=<ca_path>`` if the cloud requires it.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from edge.data_sync.retry_logic import compute_backoff_jitter
from edge.qc_inference.cache.model_cache import ModelCache
from edge.qc_inference.cache.sqlite_manager import EdgeCache

BATCH_SIZE = 100
REQUEST_TIMEOUT = 30.0
DEFAULT_SYNC_INTERVAL = 300  # 5 minutes
MAX_UPLOAD_ATTEMPTS = 10


class CloudUploader:
    """Synchronises a single edge node with the QConnect-AI cloud."""

    def __init__(
        self,
        cloud_url: str,
        lab_id: str,
        auth_token: str,
        cache: EdgeCache,
        *,
        verify: bool | str = True,
        max_attempts: int = MAX_UPLOAD_ATTEMPTS,
    ) -> None:
        """Configure the uploader.

        Args:
            cloud_url: base URL of the cloud API (no trailing slash needed).
            lab_id: this lab's identifier.
            auth_token: per-lab JWT used for ``Authorization: Bearer``.
            cache: the shared :class:`EdgeCache`.
            verify: TLS verification - True (default), or a CA bundle path for mTLS.
            max_attempts: backoff attempts per network operation.
        """
        self.cloud_url = cloud_url.rstrip("/")
        self.lab_id = lab_id
        self.auth_token = auth_token
        self.cache = cache
        self.model_cache = ModelCache(cache)
        self.verify = verify
        self.max_attempts = max_attempts
        self._last_sync: str | None = None

    @property
    def last_sync(self) -> str | None:
        """ISO timestamp of the last successful upload cycle (or ``None``)."""
        return self._last_sync

    def _headers(self) -> dict[str, str]:
        """Standard auth + content headers for cloud requests."""
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "X-Lab-Id": self.lab_id,
        }

    # ------------------------------------------------------------------ #
    # Upload
    # ------------------------------------------------------------------ #
    async def sync_pending_uploads(self) -> dict[str, Any]:
        """Upload all pending QC results in batches.

        Returns:
            A metrics dict: ``uploaded``, ``batches``, ``bytes``,
            ``duration_seconds``, ``offline`` (True if the cloud was unreachable).
        """
        started = time.perf_counter()
        total_uploaded = 0
        total_bytes = 0
        batches = 0

        if not self.cloud_url or not self.auth_token:
            logger.warning("CloudUploader: cloud_url/token not configured; skipping upload")
            return self._metrics(0, 0, 0, started, offline=True)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, verify=self.verify) as client:
            while True:
                pending = self.cache.get_pending_uploads(limit=BATCH_SIZE)
                if not pending:
                    break
                row_ids = [int(r["id"]) for r in pending]
                payload, size = self._build_batch_payload(pending)

                ok = await self._upload_batch(client, payload)
                if not ok:
                    # Leave the batch in the cache for the next cycle.
                    logger.warning(
                        "CloudUploader: batch upload failed; {} records remain queued",
                        len(row_ids),
                    )
                    return self._metrics(
                        total_uploaded, total_bytes, batches, started, offline=True
                    )

                self.cache.mark_uploaded(row_ids)
                total_uploaded += len(row_ids)
                total_bytes += size
                batches += 1
                logger.info("CloudUploader: uploaded batch of {} records", len(row_ids))

                if len(pending) < BATCH_SIZE:
                    break  # drained

        self._last_sync = datetime.now(timezone.utc).isoformat()
        metrics = self._metrics(total_uploaded, total_bytes, batches, started, offline=False)
        logger.info("CloudUploader: sync complete {}", metrics)
        return metrics

    async def _upload_batch(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> bool:
        """POST a single batch with backoff; True on 2xx, False if unrecoverable."""
        url = f"{self.cloud_url}/api/v1/labs/{self.lab_id}/qc/batch"
        for attempt in range(self.max_attempts):
            try:
                resp = await client.post(url, json=payload, headers=self._headers())
                if 200 <= resp.status_code < 300:
                    return True
                if resp.status_code in (401, 403):
                    logger.error(
                        "CloudUploader: auth rejected ({}); not retrying", resp.status_code
                    )
                    return False
                if 400 <= resp.status_code < 500:
                    logger.error(
                        "CloudUploader: client error {} ({}); not retrying",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return False
                logger.warning("CloudUploader: server error {}; will retry", resp.status_code)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                logger.warning("CloudUploader: network error (offline?): {}", exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("CloudUploader: unexpected upload error: {}", exc)
                return False

            if attempt < self.max_attempts - 1:
                import asyncio

                wait = compute_backoff_jitter(attempt, base=60.0)
                logger.info("CloudUploader: retrying batch in {:.0f}s", wait)
                await asyncio.sleep(wait)
        return False

    def _build_batch_payload(self, pending: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
        """Shape pending rows into a :class:`QCBatchUpload`-compatible payload."""
        records = []
        for r in pending:
            records.append(
                {
                    "row_id": r.get("id"),
                    "lab_id": r.get("lab_id"),
                    "analyzer_id": r.get("analyzer_id"),
                    "analyte_code": r.get("analyte_code"),
                    "qc_lot_id": r.get("qc_lot_id"),
                    "result_value": r.get("result_value"),
                    "qc_status": r.get("qc_status"),
                    "evaluation_result": r.get("evaluation_result"),
                    "timestamp": r.get("timestamp"),
                }
            )
        payload = {
            "lab_id": self.lab_id,
            "records": records,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        import json

        size = len(json.dumps(payload, default=str).encode("utf-8"))
        return payload, size

    # ------------------------------------------------------------------ #
    # Pull: models & control limits
    # ------------------------------------------------------------------ #
    async def pull_model_updates(self) -> dict[str, Any]:
        """Check cloud model versions and download any that changed.

        Returns:
            Metrics dict with ``updated`` (list of model names) and ``offline``.
        """
        if not self.cloud_url or not self.auth_token:
            return {"updated": [], "offline": True}
        updated: list[str] = []
        url = f"{self.cloud_url}/api/v1/labs/{self.lab_id}/models"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, verify=self.verify) as client:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    logger.warning("pull_model_updates: cloud returned {}", resp.status_code)
                    return {"updated": [], "offline": False}
                models = resp.json().get("models", [])
                for m in models:
                    name = m.get("name")
                    version = m.get("version")
                    if not name or not self.model_cache.needs_update(name, version):
                        continue
                    blob = await self._download_model(client, name)
                    if blob is not None:
                        self.model_cache.put(name, blob, version)
                        updated.append(name)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            logger.warning("pull_model_updates: offline ({})", exc)
            return {"updated": updated, "offline": True}
        except Exception as exc:  # noqa: BLE001
            logger.error("pull_model_updates failed: {}", exc)
            return {"updated": updated, "offline": False}
        if updated:
            logger.info("pull_model_updates: refreshed models {}", updated)
        return {"updated": updated, "offline": False}

    async def _download_model(self, client: httpx.AsyncClient, name: str) -> bytes | None:
        """Download a model binary by name (returns ``None`` on failure)."""
        url = f"{self.cloud_url}/api/v1/labs/{self.lab_id}/models/{name}/download"
        try:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 200:
                return resp.content
            logger.warning("model download {} returned {}", name, resp.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("model download {} failed: {}", name, exc)
        return None

    async def pull_control_limits(self) -> dict[str, Any]:
        """Fetch the latest control limits and cache them (TTL handled on read).

        Returns:
            Metrics dict with ``cached`` (count) and ``offline``.
        """
        if not self.cloud_url or not self.auth_token:
            return {"cached": 0, "offline": True}
        url = f"{self.cloud_url}/api/v1/labs/{self.lab_id}/control-limits"
        cached = 0
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, verify=self.verify) as client:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    logger.warning("pull_control_limits: cloud returned {}", resp.status_code)
                    return {"cached": 0, "offline": False}
                for item in resp.json().get("limits", []):
                    code = item.get("analyte_code")
                    if code:
                        self.cache.save_control_limits(code, item)
                        cached += 1
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            logger.warning("pull_control_limits: offline ({})", exc)
            return {"cached": cached, "offline": True}
        except Exception as exc:  # noqa: BLE001
            logger.error("pull_control_limits failed: {}", exc)
            return {"cached": cached, "offline": False}
        logger.info("pull_control_limits: cached limits for {} analytes", cached)
        return {"cached": cached, "offline": False}

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    async def sync_loop(self, interval_seconds: int = DEFAULT_SYNC_INTERVAL) -> None:
        """Run the full sync cycle forever on a fixed interval.

        Each cycle: pull limits, pull models, then upload pending results. Every
        step is offline-safe; the loop never exits on error.
        """
        import asyncio

        logger.info(
            "CloudUploader.sync_loop started (interval={}s, cloud={})",
            interval_seconds,
            self.cloud_url or "<unconfigured>",
        )
        while True:
            try:
                await self.pull_control_limits()
                await self.pull_model_updates()
                await self.sync_pending_uploads()
            except Exception as exc:  # noqa: BLE001 - loop must never die
                logger.exception("sync_loop cycle error: {}", exc)
            await asyncio.sleep(interval_seconds)

    @staticmethod
    def _metrics(
        uploaded: int, byte_count: int, batches: int, started: float, *, offline: bool
    ) -> dict[str, Any]:
        """Assemble a metrics dict for a sync cycle."""
        return {
            "uploaded": uploaded,
            "batches": batches,
            "bytes": byte_count,
            "duration_seconds": round(time.perf_counter() - started, 4),
            "offline": offline,
        }
