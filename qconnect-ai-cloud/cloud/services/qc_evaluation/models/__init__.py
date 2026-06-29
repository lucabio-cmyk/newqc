"""QC evaluation analytical engines.

Each engine is dependency-light (numpy/scipy optional) so the suite runs in unit
tests and on the edge without a scientific stack.
"""

from __future__ import annotations

from .distribution import DistributionDetector
from .qconnect import QConnectEngine
from .sigma import SigmaEngine
from .westgard import WestgardEngine

__all__ = [
    "WestgardEngine",
    "QConnectEngine",
    "SigmaEngine",
    "DistributionDetector",
]
