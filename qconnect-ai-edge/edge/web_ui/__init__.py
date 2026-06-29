"""Local Flask dashboard for the QConnect-AI edge node.

A richer operator-facing UI than the inference service's built-in page: it reads
the same local SQLite cache and renders per-analyte pass/fail, sync status and a
Levey-Jennings chart drawn entirely with inline vanilla JS (no external CDNs, so
it works in an air-gapped lab).
"""

from __future__ import annotations
