"""LSTM-lite 48-hour QC failure forecaster (optional ML, with heuristic fallback).

The cloud trains a small LSTM that, given a control's recent trajectory,
estimates the probability that the analyte will *fail QC within the next 48
hours*. The trained model is exported to TFLite and synced to the edge. Running
the network requires a runtime (``tflite_runtime`` or ``tensorflow``) which is an
**optional** dependency on the edge node.

Graceful degradation
---------------------
If no runtime is installed, or no model file has been synced, :class:`LSTMLite`
falls back to a fully deterministic, dependency-free heuristic based on the
trend (drift slope) and dispersion of the recent series. The fallback is
explainable and always returns a probability, so the edge never has a "hole" in
its forecast even when fully offline and ML-less. ``predict`` reports
``model_used`` so callers know whether the answer came from the network or the
heuristic.
"""

from __future__ import annotations

import math
from typing import Sequence

from loguru import logger

# Horizon, in hours, that the forecast covers.
FORECAST_HORIZON_HOURS = 48
# Number of evenly spaced points reported in the timeline.
TIMELINE_STEPS = 8


class LSTMLite:
    """Thin wrapper over an optional TFLite/TF model with a heuristic fallback."""

    def __init__(self) -> None:
        #: True once a real model + runtime are available.
        self.available: bool = False
        #: The loaded interpreter/model object, if any.
        self._interpreter: object | None = None
        #: Name of the runtime backing :attr:`available` (for diagnostics).
        self.backend: str | None = None
        self.model_path: str | None = None

    # ------------------------------------------------------------------ #
    # Model loading (guarded optional import)
    # ------------------------------------------------------------------ #
    def load_model(self, path: str) -> bool:
        """Attempt to load a TFLite model from ``path``.

        The TFLite/TensorFlow import is guarded: any ImportError (runtime not
        installed) or load error leaves the engine in fallback mode rather than
        raising, so importing this module never pulls in heavy ML deps.

        Args:
            path: filesystem path to a ``.tflite`` model.

        Returns:
            True if a real model was loaded, False if running in fallback mode.
        """
        self.model_path = path
        # Try the lightweight runtime first, then full TensorFlow.
        interpreter_cls = None
        backend = None
        try:  # pragma: no cover - exercised only where tflite is installed
            from tflite_runtime.interpreter import Interpreter as interpreter_cls  # type: ignore

            backend = "tflite_runtime"
        except Exception:  # noqa: BLE001 - any failure -> try next backend
            try:  # pragma: no cover - exercised only where tensorflow is installed
                from tensorflow.lite import Interpreter as interpreter_cls  # type: ignore

                backend = "tensorflow"
            except Exception:  # noqa: BLE001
                interpreter_cls = None

        if interpreter_cls is None:
            logger.info(
                "LSTMLite: no TFLite/TensorFlow runtime available; "
                "using deterministic heuristic fallback."
            )
            self.available = False
            return False

        try:  # pragma: no cover - requires real runtime + file
            interpreter = interpreter_cls(model_path=path)
            interpreter.allocate_tensors()
            self._interpreter = interpreter
            self.backend = backend
            self.available = True
            logger.info("LSTMLite: loaded model from {} via {}", path, backend)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LSTMLite: failed to load model {} ({}); falling back to heuristic.",
                path,
                exc,
            )
            self.available = False
            self._interpreter = None
            return False

    # ------------------------------------------------------------------ #
    # Prediction
    # ------------------------------------------------------------------ #
    def predict(self, history: Sequence[float] | None = None) -> dict:
        """Estimate the 48h failure probability for an analyte.

        Args:
            history: recent control values, oldest first.

        Returns:
            Dict with ``failure_probability_48h`` (float in [0,1]),
            ``timeline_hours`` (list of ``{hour, probability}``) and
            ``model_used`` ("lstm" or "heuristic").
        """
        hist = [float(x) for x in (history or [])]

        if self.available and self._interpreter is not None:
            try:  # pragma: no cover - requires real runtime
                prob = self._predict_with_model(hist)
                return self._build_result(prob, model_used="lstm")
            except Exception as exc:  # noqa: BLE001
                logger.warning("LSTMLite inference failed ({}); using heuristic.", exc)

        prob = self._heuristic_probability(hist)
        return self._build_result(prob, model_used="heuristic")

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _predict_with_model(self, hist: list[float]) -> float:  # pragma: no cover
        """Run the real interpreter. Shape/normalisation are model-specific."""
        # NOTE: kept minimal; the concrete tensor wiring depends on how the
        # cloud exports the model. This branch only runs when a real model is
        # present, which is never the case in the dependency-light test env.
        raise NotImplementedError("Concrete TFLite tensor wiring is deployment-specific")

    @staticmethod
    def _heuristic_probability(hist: list[float]) -> float:
        """Deterministic drift/dispersion heuristic in [0, 1].

        Intuition: a series that is *trending* (consistent slope) and/or
        *increasingly dispersed* is more likely to drift out of control within
        the next two days. We combine a normalised trend slope with the
        coefficient of variation to produce a probability.
        """
        n = len(hist)
        if n < 3:
            # Too little signal to forecast; report a low baseline risk.
            return 0.05

        mu = sum(hist) / n
        # --- Trend: least-squares slope over index, normalised by spread. -- #
        xs = list(range(n))
        x_mean = sum(xs) / n
        denom = sum((x - x_mean) ** 2 for x in xs)
        slope = 0.0
        if denom:
            slope = sum((x - x_mean) * (y - mu) for x, y in zip(xs, hist)) / denom

        ss = sum((y - mu) ** 2 for y in hist)
        sd = math.sqrt(ss / (n - 1))
        scale = sd if sd else (abs(mu) if mu else 1.0)
        # Drift across the whole window expressed in "spread units".
        drift = abs(slope) * (n - 1) / scale if scale else 0.0

        # --- Dispersion: coefficient of variation. ------------------------ #
        cv = (sd / abs(mu)) if mu else 0.0

        # --- Recent excursion: |z| of the latest point vs the window. ----- #
        last_z = abs(hist[-1] - mu) / sd if sd else 0.0

        # Logistic squashing of a weighted combination into (0, 1).
        raw = 1.2 * drift + 0.8 * cv + 0.5 * (last_z / 3.0)
        prob = 1.0 / (1.0 + math.exp(-(raw - 1.0)))
        return max(0.0, min(1.0, prob))

    @staticmethod
    def _build_result(prob: float, *, model_used: str) -> dict:
        """Shape the probability into the public result with a timeline."""
        prob = max(0.0, min(1.0, float(prob)))
        step = FORECAST_HORIZON_HOURS / TIMELINE_STEPS
        timeline: list[dict[str, float]] = []
        for i in range(1, TIMELINE_STEPS + 1):
            hour = round(step * i, 2)
            # Risk accrues monotonically across the horizon.
            frac = i / TIMELINE_STEPS
            timeline.append(
                {"hour": hour, "probability": round(prob * frac, 4)}
            )
        return {
            "failure_probability_48h": round(prob, 4),
            "timeline_hours": timeline,
            "model_used": model_used,
        }
