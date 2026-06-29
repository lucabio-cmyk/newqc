"""Thin model-blob cache layered over :class:`EdgeCache`.

The edge keeps ML model binaries (e.g. the LSTM-lite TFLite file) in SQLite so
the node can restart fully offline. ``ModelCache`` adds:

* an in-memory layer so repeated lookups don't re-read the BLOB from disk;
* version comparison helpers so the sync daemon only re-downloads on a mismatch;
* a helper to materialise a cached blob onto disk for runtimes that load by path.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from loguru import logger

from edge.qc_inference.cache.sqlite_manager import EdgeCache


class ModelCache:
    """In-memory + SQLite cache for model binaries and their versions."""

    def __init__(self, cache: EdgeCache) -> None:
        """Wrap an existing :class:`EdgeCache`.

        Args:
            cache: the shared SQLite cache instance.
        """
        self._cache = cache
        # name -> {"binary": bytes, "version": str, "last_updated": str}
        self._mem: dict[str, dict[str, Any]] = {}

    def get_version(self, name: str) -> str | None:
        """Return the cached version string for ``name`` (or ``None``)."""
        if name in self._mem:
            return self._mem[name].get("version")
        record = self._cache.get_model(name)
        if record is None:
            return None
        self._mem[name] = record
        return record.get("version")

    def needs_update(self, name: str, latest_version: str | None) -> bool:
        """True if there is no cached model or the cloud version differs.

        Args:
            name: model name.
            latest_version: version advertised by the cloud (``None`` -> unknown).
        """
        if latest_version is None:
            return False  # cannot compare; do not thrash the network
        return self.get_version(name) != latest_version

    def put(self, name: str, binary: bytes, version: str) -> None:
        """Persist a model blob to SQLite and refresh the in-memory copy."""
        self._cache.save_model(name, binary, version)
        self._mem[name] = {
            "name": name,
            "binary": binary,
            "version": version,
        }
        logger.info("ModelCache: stored model {} v{} ({} bytes)", name, version, len(binary))

    def get(self, name: str) -> bytes | None:
        """Return the raw model bytes for ``name`` (or ``None`` if absent)."""
        if name in self._mem and self._mem[name].get("binary"):
            return self._mem[name]["binary"]
        record = self._cache.get_model(name)
        if record is None:
            return None
        self._mem[name] = record
        return record.get("binary")

    def materialize(self, name: str, dest_dir: str | None = None) -> str | None:
        """Write the cached blob to a file and return its path (or ``None``).

        Some runtimes (TFLite) load models by filesystem path rather than from
        memory, so we spill the BLOB to a temp file on demand.

        Args:
            name: model name.
            dest_dir: directory to write into (defaults to the system temp dir).
        """
        binary = self.get(name)
        if not binary:
            return None
        dest_dir = dest_dir or tempfile.gettempdir()
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, f"{name}.tflite")
        try:
            with open(path, "wb") as fh:
                fh.write(binary)
            logger.debug("ModelCache: materialized {} -> {}", name, path)
            return path
        except OSError as exc:
            logger.error("ModelCache.materialize failed for {}: {}", name, exc)
            return None
