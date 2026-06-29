"""Flask local dashboard for the edge node.

Serves an operator dashboard and a small JSON API backed by the same SQLite
cache the inference service writes to. Everything degrades gracefully: if the
cache is unavailable the API returns sensible stubs so the page still renders.

No external CDNs are used anywhere - all CSS/JS is inline in the templates so the
dashboard works fully offline.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
LAB_ID = os.getenv("LAB_ID", "lab-unknown")
LAB_NAME = os.getenv("LAB_NAME", "Unnamed Laboratory")
DB_PATH = os.getenv("DB_PATH", "/data/qc_cache.db")
CONFIG_PATH = os.getenv("LAB_CONFIG_PATH", "/data/lab_config.json")


def _open_cache() -> Any | None:
    """Open the shared EdgeCache, returning ``None`` if unavailable.

    Imported lazily so the web UI starts even if the inference package or its DB
    is not present yet.
    """
    try:
        from edge.qc_inference.cache.sqlite_manager import EdgeCache

        if not os.path.exists(DB_PATH) and DB_PATH != ":memory:":
            return None
        return EdgeCache(DB_PATH)
    except Exception:  # noqa: BLE001 - any failure -> stub mode
        return None


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.route("/")
def index() -> str:
    """Render the dashboard page."""
    return render_template("dashboard.html", lab_id=LAB_ID, lab_name=LAB_NAME)


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
@app.route("/api/status")
def api_status():
    """Return overall edge status for the dashboard."""
    cache = _open_cache()
    pending = 0
    qc_24h: list[dict[str, Any]] = []
    models = {"lstm": False, "rf": False, "version": None}
    if cache is not None:
        try:
            pending = cache.count_pending_uploads()
            qc_24h = [
                {"analyte": r.get("analyte"), "pass": r.get("pass", 0), "fail": r.get("fail", 0)}
                for r in cache.get_qc_summary_24h()
            ]
            cached_models = cache.list_models()
            if cached_models:
                names = {m["model_name"] for m in cached_models}
                models["lstm"] = "lstm_lite" in names
                models["rf"] = "random_forest" in names
                models["version"] = next((m.get("version") for m in cached_models), None)
        finally:
            cache.close()

    return jsonify(
        {
            "lab_id": LAB_ID,
            "lab_name": LAB_NAME,
            # The web UI cannot itself reach the cloud; report unknown/false and
            # let the inference /status be the source of truth for connectivity.
            "cloud_connected": False,
            "pending_uploads": pending,
            "last_sync": None,
            "qc_24h": qc_24h,
            "models_loaded": models,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.route("/api/qc-history/<analyte_code>")
def api_qc_history(analyte_code: str):
    """Return Levey-Jennings data (last 30 points + mean/SD lines) for an analyte."""
    cache = _open_cache()
    values: list[float] = []
    if cache is not None:
        try:
            values = cache.get_local_qc_history(analyte_code, days=30)[-30:]
        finally:
            cache.close()

    mean, sd = _mean_sd(values)
    points = [{"index": i, "value": v} for i, v in enumerate(values)]
    return jsonify(
        {
            "analyte_code": analyte_code,
            "points": points,
            "mean": mean,
            "sd": sd,
            "lines": {
                "mean": mean,
                "plus_1sd": mean + sd,
                "minus_1sd": mean - sd,
                "plus_2sd": mean + 2 * sd,
                "minus_2sd": mean - 2 * sd,
                "plus_3sd": mean + 3 * sd,
                "minus_3sd": mean - 3 * sd,
            },
        }
    )


@app.route("/settings", methods=["POST"])
def settings():
    """Update lab configuration (stub: persists posted JSON to a config file)."""
    payload = request.get_json(silent=True) or {}
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        return jsonify({"saved": True, "path": CONFIG_PATH})
    except OSError as exc:
        return jsonify({"saved": False, "error": str(exc)}), 500


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _mean_sd(values: list[float]) -> tuple[float, float]:
    """Return (mean, sample SD) for a value list; (0,0) if too short."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mu = sum(values) / n
    if n < 2:
        return round(mu, 6), 0.0
    ss = sum((x - mu) ** 2 for x in values)
    sd = math.sqrt(ss / (n - 1))
    return round(mu, 6), round(sd, 6)


if __name__ == "__main__":
    # Bind on all interfaces inside the container; the compose file maps 8001.
    app.run(host="0.0.0.0", port=int(os.getenv("WEB_UI_PORT", "8001")))
