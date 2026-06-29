# QConnect-AI Cloud — API Reference

Base URL (qc-evaluation): `http://localhost:8000`
API prefix: `/api/v1`
Interactive docs: `GET /docs` (Swagger), `GET /redoc`.

All requests/responses are JSON. Every response echoes an `X-Correlation-ID`
header (generated if absent). When `REQUIRE_AUTH=true`, send
`Authorization: Bearer <jwt>`.

---

## qc-evaluation service (:8000)

### `GET /health`

Liveness/readiness probe. Never fails on dependency outage — degraded checks are
reported as `false`.

**Response 200**
```json
{
  "status": "ok",
  "service": "qc-evaluation",
  "version": "0.1.0",
  "checks": { "db": false, "redis": false },
  "timestamp": "2026-06-29T12:00:00.000000+00:00"
}
```

---

### `POST /api/v1/qc/evaluate`

Evaluate a single QC result through all engines + AI and return the fused
verdict.

**Request body** (`QCDataInput`)
```json
{
  "lab_id": "lab-genova-001",
  "analyzer_id": "ABBOTT-ARCHITECT-001",
  "analyte_code": "HCV-AB",
  "analyte_type": "serology",
  "qc_lot_id": "QC-HCV-DIAMEX-202603-001",
  "qc_level": "NORMAL",
  "result_value": 1.45,
  "target_value": 1.50,
  "sd_value": 0.08,
  "operator_id": "EMP00234",
  "correlation_id": "9f1c2b3d4e5f6a7b8c9d0e1f2a3b4c5d",
  "timestamp": "2026-03-15T09:30:00Z"
}
```

Field notes: `analyte_type` ∈ `serology|chemistry|hematology|coagulation|nat`;
`qc_level` ∈ `LOW|NORMAL|HIGH|CUSTOM`; `result_value > 0`; `sd_value > 0`;
`timestamp` defaults to now (tz-aware) if omitted.

**Response 200** (`QCEvaluationResponse`)
```json
{
  "qc_status": "PASS",
  "severity": "LOW",
  "legacy_results": {
    "westgard": {
      "status": "PASS", "rule_violated": null,
      "rules_checked": ["1-3S","2-2S","R-4S","4-1S","10x","7T","1-2S"],
      "mean": 1.5, "sd": 0.08, "cv_percent": 5.33, "deviation_sd": -0.625
    },
    "qconnect": {
      "status": "PASS", "percentile_5": 1.45, "percentile_95": 1.45,
      "percentile_position": 0.5, "lcl": 1.45, "ucl": 1.45
    },
    "sigma": {
      "sigma_metric": 5.0, "sigma_category": "4-6",
      "recommended_rules": "1-3S / 2-2S / R-4S (N=2)", "expected_frr_percent": 1.0
    },
    "distribution": {
      "detected_distribution": "unknown", "confidence": 0.0,
      "recommended_qc_approach": "westgard", "sample_size": 0
    }
  },
  "ai_insights": {
    "distribution_type": "unknown",
    "failure_probability_48h": 0.12,
    "anomaly_detected": false,
    "anomaly_score": 0.16,
    "diagnostic_sigma": null,
    "clinical_impact_percent": 3.1,
    "recommended_action": null
  },
  "recommendation": "In control. Continue routine operation.",
  "confidence": 0.1,
  "correlation_id": "9f1c2b3d4e5f6a7b8c9d0e1f2a3b4c5d",
  "evaluated_offline": false,
  "timestamp": "2026-06-29T12:00:00.000000+00:00"
}
```

`qc_status` ∈ `PASS | FAIL | REVIEW_REQUIRED | HOLD_PENDING_AI`.
`severity` ∈ `CRITICAL | HIGH | MEDIUM | LOW`.

**Errors**
* `422` — validation failure: `{ "error": "validation_error", "message": "...", "details": [...] }`
* `500` — engine error: `{ "error": "engine_error" | "internal_error", "message": "..." }`

---

### `POST /api/v1/labs/{lab_id}/qc/batch`

The edge sync target: ingest a batch of QC results. Each record is evaluated and
persisted best-effort; a record that fails is counted as rejected without
aborting the batch.

**Request body** (`QCBatchUpload`)
```json
{
  "lab_id": "lab-genova-001",
  "records": [ { /* QCDataInput */ }, { /* QCDataInput */ } ],
  "sent_at": "2026-03-15T09:35:00Z"
}
```
`records`: 1–1000 items.

**Response 200** (`QCBatchAccepted`)
```json
{
  "lab_id": "lab-genova-001",
  "accepted": 2,
  "rejected": 0,
  "correlation_id": "…",
  "received_at": "2026-06-29T12:00:00.000000+00:00"
}
```

---

### `GET /api/v1/labs/{lab_id}/qc/status`

Recent QC status summary for a lab. **Placeholder**: aggregates counts from
`qc_results` over a rolling window; returns zeros when no DB is configured.

**Response 200** (`LabQCStatusSummary`)
```json
{
  "lab_id": "lab-genova-001",
  "window_hours": 24,
  "total_results": 0,
  "pass_count": 0,
  "fail_count": 0,
  "review_required_count": 0,
  "hold_pending_ai_count": 0,
  "last_evaluated_at": null,
  "generated_at": "2026-06-29T12:00:00.000000+00:00"
}
```

---

## ml-inference service (:8001)

### `POST /predict`
Returns an AIInsights-compatible payload. Accepts the full `QCDataInput` (extra
fields ignored).

**Response 200**
```json
{
  "distribution_type": "unknown",
  "failure_probability_48h": 0.12,
  "anomaly_detected": false,
  "anomaly_score": 0.16,
  "clinical_impact_percent": 3.1,
  "recommended_action": null,
  "model_version": "0.1.0"
}
```

### `GET /health` → `{ "status": "ok", "service": "ml-inference", ... }`

---

## federated-learning service (:8002)

### `GET /api/v1/federated/round/current` (placeholder)
```json
{
  "round_number": 0, "participating_labs": 0,
  "global_model_version": "v0", "differential_privacy_epsilon": 1.0,
  "global_model_accuracy": null, "global_model_auc": null,
  "aggregation_timestamp": "2026-06-29T12:00:00+00:00"
}
```
### `GET /health`

---

## rca-capa service (:8003)

### `POST /api/v1/rca/suggest` (placeholder)
**Request** `{ "analyte_code": "HCV-AB", "westgard_rule_violated": "2-2S", "severity": "HIGH" }`

**Response 200**
```json
{
  "candidates": [
    { "category": "calibration", "description": "Calibration drift / shifted lot setpoint", "confidence": 0.6 },
    { "category": "reagent", "description": "New reagent lot bias", "confidence": 0.35 }
  ],
  "model_note": "placeholder graph-free heuristic"
}
```
### `GET /health`

---

## analytics service (:8004)

### `GET /api/v1/analytics/kpis?window_days=30` (placeholder)
```json
{
  "window_days": 30, "total_qc_runs": 0, "pass_rate_percent": 0.0,
  "mean_sigma": 0.0, "open_capa_actions": 0,
  "generated_at": "2026-06-29T12:00:00+00:00"
}
```
### `GET /health`
