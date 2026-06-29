"""In-memory federated round manager.

Tracks training rounds, collects per-lab weight updates, and runs DP-FedAvg
aggregation to advance a single global model. This is a single-process
simulation, so no locking is performed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .aggregation import dp, fedavg


class RoundManager:
    """Manage federated rounds and the evolving global model."""

    def __init__(self) -> None:
        self._round_number = 0
        # round_number -> {lab_id: update}
        self._updates: dict[int, dict[str, dict[str, Any]]] = {}
        self._global_weights: list[float] | None = None
        self._model_version = 0
        self._updated_at: datetime | None = None
        self._aggregated_rounds: set[int] = set()

    # -- rounds ---------------------------------------------------------------
    def start_round(self) -> int:
        """Open a new round and return its number."""
        self._round_number += 1
        self._updates[self._round_number] = {}
        return self._round_number

    def submit_update(self, lab_id: str, update: dict[str, Any], round_number: int) -> int:
        """Record ``lab_id``'s update for ``round_number``; return total updates.

        Re-submission by the same lab in the same round overwrites the previous
        update. Raises ``ValueError`` if the round does not exist or has already
        been aggregated.
        """
        if round_number not in self._updates:
            raise ValueError(f"round {round_number} does not exist")
        if round_number in self._aggregated_rounds:
            raise ValueError(f"round {round_number} already aggregated")
        # Validate vector against any existing global model dimension.
        if self._global_weights is not None and len(update["weights"]) != len(self._global_weights):
            raise ValueError(
                f"weights length {len(update['weights'])} does not match "
                f"global model dimension {len(self._global_weights)}"
            )
        self._updates[round_number][lab_id] = update
        return len(self._updates[round_number])

    def aggregate_round(
        self,
        round_number: int,
        epsilon: float,
        delta: float,
        clip_norm: float,
    ) -> dict[str, Any]:
        """Aggregate a round with clipping + DP noise and advance the model.

        Steps: clip each update to ``clip_norm`` (L2), FedAvg by sample count,
        add Gaussian noise calibrated for ``(epsilon, delta)`` with sensitivity
        ``clip_norm``, then set the result as the new global model.
        """
        if round_number not in self._updates:
            raise ValueError(f"round {round_number} does not exist")
        if round_number in self._aggregated_rounds:
            raise ValueError(f"round {round_number} already aggregated")

        round_updates = self._updates[round_number]
        if not round_updates:
            raise ValueError(f"round {round_number} has no submitted updates")

        clipped: list[dict[str, Any]] = []
        for lab_id, upd in round_updates.items():
            clipped.append(
                {
                    "weights": dp.clip_l2(upd["weights"], clip_norm),
                    "num_samples": upd["num_samples"],
                }
            )

        aggregated = fedavg.federated_average(clipped)

        sigma = dp.gaussian_sigma_for(epsilon, delta, sensitivity=clip_norm)
        if sigma > 0:
            # Seed with the round number for deterministic, reproducible runs.
            aggregated = dp.add_gaussian_noise(aggregated, sigma, seed=round_number)

        self._global_weights = aggregated
        self._model_version += 1
        self._updated_at = datetime.now(timezone.utc)
        self._aggregated_rounds.add(round_number)

        return {
            "round_number": round_number,
            "participating_labs": len(round_updates),
            "global_model_version": self._model_version,
            "global_weights": list(self._global_weights),
            "dp_epsilon": epsilon,
            "dp_sigma": sigma,
        }

    # -- model accessors ------------------------------------------------------
    def get_global_model(self) -> list[float] | None:
        """Return the current global weights (or ``None`` if not yet aggregated)."""
        return None if self._global_weights is None else list(self._global_weights)

    @property
    def model_version(self) -> int:
        return self._model_version

    @property
    def updated_at(self) -> datetime | None:
        return self._updated_at

    @property
    def current_round(self) -> int:
        return self._round_number
