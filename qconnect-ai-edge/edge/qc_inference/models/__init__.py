"""Local inference engines for the QConnect-AI edge node.

All engines are pure-Python and dependency-light so they evaluate within the
sub-100ms edge budget and run on constrained hardware without scientific stacks.

Engines
-------
* :class:`~edge.qc_inference.models.westgard.WestgardEngine` - classic multirules.
* :class:`~edge.qc_inference.models.qconnect.QConnectEngine` - percentile limits.
* :class:`~edge.qc_inference.models.lstm_lite.LSTMLite` - 48h failure forecast.
* :class:`~edge.qc_inference.models.anomaly.AnomalyDetector` - statistical anomalies.
"""

from __future__ import annotations
