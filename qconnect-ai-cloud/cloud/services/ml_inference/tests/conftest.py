"""Test config: put the service directory on ``sys.path``.

The service is deployed with ``uvicorn main:app`` from its own directory, so
``main`` imports ``from models import ...`` as top-level modules. To mirror that
layout under pytest we prepend the service directory (the parent of this
``tests`` dir) to ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))
