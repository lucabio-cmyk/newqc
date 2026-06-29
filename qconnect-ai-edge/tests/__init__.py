"""Test suite for the QConnect-AI edge node.

Tests are restricted to pure-stdlib + pydantic modules (engines, cache, HL7,
schemas) so they run without FastAPI/TensorFlow/numpy installed.
"""

from __future__ import annotations
