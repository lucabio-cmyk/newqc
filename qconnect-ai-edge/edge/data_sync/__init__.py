"""Edge -> cloud synchronisation daemon.

Runs as a separate container next to the inference service. It drains the local
SQLite cache of pending QC results, uploads them in batches, and pulls down
model and control-limit updates - all with exponential-backoff retry so the edge
keeps working through outages and catches up when connectivity returns.
"""

from __future__ import annotations
