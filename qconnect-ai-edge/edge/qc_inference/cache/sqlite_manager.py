"""Offline-first SQLite cache for the QConnect-AI edge node.

This is the durability backbone that makes the edge node *offline-capable*: every
evaluated QC result is written here as "pending upload" before the response is
returned, so nothing is lost if the cloud is unreachable. Control limits and
model blobs synced from the cloud are also cached here so evaluation works with
zero network access.

Design notes
------------
* Pure standard library (``sqlite3``) - no ORM, no extra deps.
* All queries are parameterised (no string interpolation of values).
* Writes are wrapped in transactions via the connection context manager.
* Result/limit dicts are JSON-serialised into TEXT columns.
* The class is a context manager so callers can ``with EdgeCache(path) as c:``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import Any

from loguru import logger

# How long cached control limits remain valid before they are considered stale.
CONTROL_LIMITS_TTL_SECONDS = 86_400  # 24 hours


def _utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class EdgeCache:
    """SQLite-backed local cache for QC results, limits, models and HL7 messages."""

    def __init__(self, db_path: str) -> None:
        """Open (and initialise) the cache database.

        Args:
            db_path: filesystem path to the SQLite file. ``:memory:`` is allowed
                for tests. Parent directories are assumed to exist.
        """
        self.db_path = db_path
        # ``check_same_thread=False`` lets the FastAPI threadpool/async tasks
        # share a single connection; we serialise access ourselves via short
        # transactions. ``isolation_level=None`` is avoided so the context
        # manager handles commit/rollback.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL improves concurrent read/write for the dashboard + ingest path.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning("EdgeCache: PRAGMA setup failed: {}", exc)
        self._init_db()
        logger.debug("EdgeCache opened at {}", db_path)

    # ------------------------------------------------------------------ #
    # Context manager
    # ------------------------------------------------------------------ #
    def __enter__(self) -> EdgeCache:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        """Create tables and indexes if they do not already exist."""
        with self._conn:  # transaction
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS qc_results_pending (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    lab_id            TEXT NOT NULL,
                    analyzer_id       TEXT NOT NULL,
                    analyte_code      TEXT NOT NULL,
                    qc_lot_id         TEXT,
                    result_value      REAL,
                    qc_status         TEXT,
                    evaluation_result TEXT,            -- JSON blob
                    timestamp         TEXT NOT NULL,
                    uploaded          INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS ix_pending_uploaded
                    ON qc_results_pending (uploaded);
                CREATE INDEX IF NOT EXISTS ix_pending_analyte_ts
                    ON qc_results_pending (analyte_code, timestamp);

                CREATE TABLE IF NOT EXISTS control_limits_cache (
                    analyte_code TEXT PRIMARY KEY,
                    limits_json  TEXT NOT NULL,
                    last_updated TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_cache (
                    model_name   TEXT PRIMARY KEY,
                    model_binary BLOB,
                    version      TEXT,
                    last_updated TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hl7_raw_messages (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_message          TEXT NOT NULL,
                    parsed_analyte_code  TEXT,
                    timestamp            TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ #
    # QC results
    # ------------------------------------------------------------------ #
    def save_qc_result(self, qc_data: dict[str, Any], result: dict[str, Any]) -> int:
        """Persist an evaluated QC result as pending upload.

        Args:
            qc_data: the inbound :class:`QCDataInput` as a dict.
            result: the :class:`QCEvaluationResponse` as a dict.

        Returns:
            The autoincrement row id of the stored record.
        """
        ts = qc_data.get("timestamp") or _utcnow_iso()
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        try:
            with self._conn:
                cur = self._conn.execute(
                    """
                    INSERT INTO qc_results_pending
                        (lab_id, analyzer_id, analyte_code, qc_lot_id,
                         result_value, qc_status, evaluation_result, timestamp, uploaded)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        str(qc_data.get("lab_id", "")),
                        str(qc_data.get("analyzer_id", "")),
                        str(qc_data.get("analyte_code", "")),
                        str(qc_data.get("qc_lot_id", "")),
                        _as_float(qc_data.get("result_value")),
                        str(result.get("qc_status", "")),
                        json.dumps(result, default=str),
                        str(ts),
                    ),
                )
            row_id = int(cur.lastrowid or 0)
            logger.debug("EdgeCache: saved pending QC result id={}", row_id)
            return row_id
        except sqlite3.Error as exc:
            logger.error("EdgeCache.save_qc_result failed: {}", exc)
            raise

    def get_local_qc_history(self, analyte_code: str, days: int = 30) -> list[float]:
        """Return recent control values for an analyte, oldest first.

        Args:
            analyte_code: analyte to query.
            days: look-back window in days.

        Returns:
            List of ``result_value`` floats ordered chronologically.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            rows = self._conn.execute(
                """
                SELECT result_value FROM qc_results_pending
                WHERE analyte_code = ? AND timestamp >= ? AND result_value IS NOT NULL
                ORDER BY timestamp ASC, id ASC
                """,
                (analyte_code, since),
            ).fetchall()
            return [float(r["result_value"]) for r in rows]
        except sqlite3.Error as exc:
            logger.error("EdgeCache.get_local_qc_history failed: {}", exc)
            return []

    def get_pending_uploads(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch up to ``limit`` not-yet-uploaded QC records (oldest first)."""
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM qc_results_pending
                WHERE uploaded = 0
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                rec = dict(r)
                # Re-hydrate the JSON evaluation payload for convenience.
                raw = rec.get("evaluation_result")
                if raw:
                    try:
                        rec["evaluation_result"] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        pass
                out.append(rec)
            return out
        except sqlite3.Error as exc:
            logger.error("EdgeCache.get_pending_uploads failed: {}", exc)
            return []

    def count_pending_uploads(self) -> int:
        """Return the number of QC records still awaiting upload."""
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM qc_results_pending WHERE uploaded = 0"
            ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.Error as exc:
            logger.error("EdgeCache.count_pending_uploads failed: {}", exc)
            return 0

    def mark_uploaded(self, row_ids: list[int]) -> int:
        """Mark the given rows as uploaded.

        Args:
            row_ids: primary keys to flag.

        Returns:
            Number of rows updated.
        """
        if not row_ids:
            return 0
        placeholders = ",".join("?" for _ in row_ids)
        try:
            with self._conn:
                cur = self._conn.execute(
                    f"UPDATE qc_results_pending SET uploaded = 1 "  # noqa: S608 - ids only
                    f"WHERE id IN ({placeholders})",
                    [int(i) for i in row_ids],
                )
            return cur.rowcount
        except sqlite3.Error as exc:
            logger.error("EdgeCache.mark_uploaded failed: {}", exc)
            raise

    def get_qc_summary_24h(self) -> list[dict[str, Any]]:
        """Per-analyte PASS/FAIL counts over the last 24 hours (for dashboards)."""
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        try:
            rows = self._conn.execute(
                """
                SELECT analyte_code AS analyte,
                       SUM(CASE WHEN qc_status = 'PASS' THEN 1 ELSE 0 END) AS pass,
                       SUM(CASE WHEN qc_status = 'FAIL' THEN 1 ELSE 0 END) AS fail,
                       SUM(CASE WHEN qc_status NOT IN ('PASS', 'FAIL') THEN 1 ELSE 0 END)
                           AS other
                FROM qc_results_pending
                WHERE timestamp >= ?
                GROUP BY analyte_code
                ORDER BY analyte_code ASC
                """,
                (since,),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error("EdgeCache.get_qc_summary_24h failed: {}", exc)
            return []

    # ------------------------------------------------------------------ #
    # Control limits cache (TTL-aware)
    # ------------------------------------------------------------------ #
    def save_control_limits(self, analyte_code: str, limits: dict[str, Any]) -> None:
        """Upsert cached control limits for an analyte (pushed from the cloud)."""
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO control_limits_cache (analyte_code, limits_json, last_updated)
                    VALUES (?, ?, ?)
                    ON CONFLICT(analyte_code) DO UPDATE SET
                        limits_json = excluded.limits_json,
                        last_updated = excluded.last_updated
                    """,
                    (analyte_code, json.dumps(limits, default=str), _utcnow_iso()),
                )
            logger.debug("EdgeCache: cached control limits for {}", analyte_code)
        except sqlite3.Error as exc:
            logger.error("EdgeCache.save_control_limits failed: {}", exc)
            raise

    def get_control_limits(
        self, analyte_code: str, ttl_seconds: int = CONTROL_LIMITS_TTL_SECONDS
    ) -> dict[str, Any] | None:
        """Return cached limits if present and fresh, else ``None``.

        Args:
            analyte_code: analyte to look up.
            ttl_seconds: maximum age before the cache entry is considered stale.

        Returns:
            The cached limits dict, or ``None`` if missing or expired.
        """
        try:
            row = self._conn.execute(
                "SELECT limits_json, last_updated FROM control_limits_cache "
                "WHERE analyte_code = ?",
                (analyte_code,),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.error("EdgeCache.get_control_limits failed: {}", exc)
            return None
        if row is None:
            return None
        if self._is_stale(row["last_updated"], ttl_seconds):
            logger.debug("EdgeCache: control limits for {} are stale", analyte_code)
            return None
        try:
            return json.loads(row["limits_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    # ------------------------------------------------------------------ #
    # Model cache
    # ------------------------------------------------------------------ #
    def save_model(self, name: str, binary: bytes, version: str) -> None:
        """Upsert a cached model blob keyed by name."""
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO model_cache (model_name, model_binary, version, last_updated)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(model_name) DO UPDATE SET
                        model_binary = excluded.model_binary,
                        version = excluded.version,
                        last_updated = excluded.last_updated
                    """,
                    (name, sqlite3.Binary(binary), version, _utcnow_iso()),
                )
            logger.debug("EdgeCache: cached model {} v{}", name, version)
        except sqlite3.Error as exc:
            logger.error("EdgeCache.save_model failed: {}", exc)
            raise

    def get_model(self, name: str) -> dict[str, Any] | None:
        """Return ``{name, binary, version, last_updated}`` for a model, or ``None``."""
        try:
            row = self._conn.execute(
                "SELECT model_name, model_binary, version, last_updated "
                "FROM model_cache WHERE model_name = ?",
                (name,),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.error("EdgeCache.get_model failed: {}", exc)
            return None
        if row is None:
            return None
        return {
            "name": row["model_name"],
            "binary": bytes(row["model_binary"]) if row["model_binary"] else b"",
            "version": row["version"],
            "last_updated": row["last_updated"],
        }

    def list_models(self) -> list[dict[str, Any]]:
        """Return metadata (name + version) for all cached models."""
        try:
            rows = self._conn.execute(
                "SELECT model_name, version, last_updated FROM model_cache"
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error("EdgeCache.list_models failed: {}", exc)
            return []

    # ------------------------------------------------------------------ #
    # Raw HL7 messages
    # ------------------------------------------------------------------ #
    def save_hl7_raw(
        self, raw_message: str, parsed_analyte_code: str | None = None
    ) -> int:
        """Archive a raw HL7 message for audit/replay; returns the row id."""
        try:
            with self._conn:
                cur = self._conn.execute(
                    """
                    INSERT INTO hl7_raw_messages (raw_message, parsed_analyte_code, timestamp)
                    VALUES (?, ?, ?)
                    """,
                    (raw_message, parsed_analyte_code, _utcnow_iso()),
                )
            return int(cur.lastrowid or 0)
        except sqlite3.Error as exc:
            logger.error("EdgeCache.save_hl7_raw failed: {}", exc)
            raise

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #
    def cleanup_old_records(self, days: int = 90) -> int:
        """Delete uploaded QC results and old HL7 messages beyond ``days``.

        Pending (not-yet-uploaded) results are never deleted so data is not lost
        during extended outages.

        Returns:
            Total number of rows removed across the affected tables.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        removed = 0
        try:
            with self._conn:
                cur1 = self._conn.execute(
                    "DELETE FROM qc_results_pending "
                    "WHERE uploaded = 1 AND timestamp < ?",
                    (cutoff,),
                )
                cur2 = self._conn.execute(
                    "DELETE FROM hl7_raw_messages WHERE timestamp < ?",
                    (cutoff,),
                )
                removed = (cur1.rowcount or 0) + (cur2.rowcount or 0)
            logger.info("EdgeCache: cleaned up {} old records", removed)
            return removed
        except sqlite3.Error as exc:
            logger.error("EdgeCache.cleanup_old_records failed: {}", exc)
            return removed

    def ping(self) -> bool:
        """Lightweight connectivity check used by the health probe."""
        try:
            self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_stale(last_updated: str, ttl_seconds: int) -> bool:
        """True if ``last_updated`` is older than ``ttl_seconds`` from now."""
        try:
            ts = datetime.fromisoformat(last_updated)
        except (ValueError, TypeError):
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > ttl_seconds


def _as_float(value: Any) -> float | None:
    """Best-effort float coercion that never raises (returns ``None`` on failure)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
