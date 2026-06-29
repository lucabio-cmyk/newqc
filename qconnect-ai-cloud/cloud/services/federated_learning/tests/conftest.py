"""Path wiring for the federated-learning test suite.

Ensures the repo root (``…/qconnect-ai-cloud``) is importable so that
``cloud.services.federated_learning`` resolves as a namespace package, with no
database or network required.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]  # …/qconnect-ai-cloud

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
