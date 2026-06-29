"""QConnect-AI shared library.

Re-exports the most commonly used symbols so callers can simply::

    from shared import QCDataInput, QCEvaluationResponse, QConnectError
"""

from shared.exceptions import (
    AuthenticationError,
    ConfigurationError,
    EvaluationError,
    QConnectError,
    UpstreamServiceError,
    ValidationError,
)
from shared.models import (
    AIInsights,
    AnalyteType,
    CAPAActionInput,
    QCDataInput,
    QCEvaluationResponse,
    QCLevelType,
    QConnectResult,
    QCStatusEnum,
    SeverityEnum,
    SigmaResult,
    WestgardResult,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # models
    "AnalyteType",
    "QCLevelType",
    "QCStatusEnum",
    "SeverityEnum",
    "QCDataInput",
    "WestgardResult",
    "QConnectResult",
    "SigmaResult",
    "AIInsights",
    "QCEvaluationResponse",
    "CAPAActionInput",
    # exceptions
    "QConnectError",
    "ValidationError",
    "AuthenticationError",
    "EvaluationError",
    "UpstreamServiceError",
    "ConfigurationError",
]
