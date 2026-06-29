# QConnect-AI Cloud — Architecture

## 1. Overview

QConnect-AI Enterprise is a two-tier QC monitoring platform for diagnostic
laboratories:

* **Edge tier** (`qconnect-ai-edge`) runs on-prem, evaluates QC results with
  sub-100ms latency even when disconnected, and syncs to the cloud.
* **Cloud tier** (this project) is the authoritative system of record: it
  re-evaluates results with the full engine + AI stack, manages control limits,
  CAPA workflows, federated learning and cross-lab analytics.

Both tiers share a single **wire contract** defined in `qconnect-ai-shared`
(`shared.models`). The cloud re-declares it locally in
`cloud/services/qc_evaluation/schemas.py` for build independence.

## 2. Components

```
                         ┌──────────────────────────────────────────────┐
   Lab analyzers  ──HL7──▶│  EDGE NODE (qconnect-ai-edge)                 │
   (Architect, …)         │  • offline Westgard/QConnect/Sigma            │
                          │  • local store + sync daemon                  │
                          └───────────────┬──────────────────────────────┘
                                          │ HTTPS batch sync (JWT, HMAC)
                                          ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │                          CLOUD TIER (qconnect-ai-cloud)                      │
 │                                                                             │
 │   ┌────────────────────────────┐        ┌───────────────────────────┐      │
 │   │  qc-evaluation  :8000       │  http  │  ml-inference  :8001       │      │
 │   │  ── orchestrator ──         │───────▶│  failure prob / anomaly    │      │
 │   │  Westgard  QConnect         │◀───────│  (best-effort, degrades)   │      │
 │   │  Sigma     Distribution     │        └───────────────────────────┘      │
 │   │  + fusion + severity        │                                           │
 │   └──┬───────────┬──────────────┘                                           │
 │      │           │                                                          │
 │      │           ├──────────────┐         ┌───────────────────────────┐    │
 │      │           ▼              ▼         │  rca-capa  :8003           │    │
 │      │   ┌─────────────┐  ┌──────────┐    │  root-cause over graph     │    │
 │      │   │  Redis      │  │ Postgres │    └─────────────┬─────────────┘    │
 │      │   │  cache      │  │ (init.sql)│                  │ bolt             │
 │      │   └─────────────┘  └────┬─────┘            ┌──────▼──────┐           │
 │      │                         │                  │   Neo4j     │           │
 │      │                  ┌──────▼──────┐           │  RCA graph  │           │
 │      │                  │ TimescaleDB │           └─────────────┘           │
 │      │                  │ time-series │                                     │
 │      │                  └─────────────┘   ┌───────────────────────────┐    │
 │      │                                    │ federated-learning :8002   │    │
 │      │             ┌──────────────┐ amqp  │  DP aggregation (FedAvg)   │    │
 │      └────────────▶│  RabbitMQ    │◀──────┤                            │    │
 │                    └──────────────┘       └───────────────────────────┘    │
 │                                                                             │
 │                    ┌───────────────────────────┐                           │
 │                    │  analytics  :8004          │  KPI / reporting          │
 │                    └───────────────────────────┘                           │
 └───────────────────────────────────────────────────────────────────────────┘
```

## 3. Data flow: edge → cloud

1. An analyzer emits a QC result (HL7) to the edge node.
2. The edge evaluates locally (offline-safe) and persists.
3. The sync daemon batches results and POSTs them to
   `POST /api/v1/labs/{lab_id}/qc/batch` (JWT auth, payload HMAC).
4. `qc-evaluation` re-runs all engines, calls `ml-inference` (best-effort),
   **fuses** the verdict and persists to Postgres (+ TimescaleDB for series).
5. A `FAIL` may auto-open a CAPA via `rca-capa`, which queries the Neo4j graph
   for likely root causes.
6. `analytics` aggregates KPIs; `federated-learning` periodically improves the
   shared models without raw data ever leaving a lab.

### Single-result fusion logic (qc-evaluation)

```
westgard, qconnect, sigma, distribution = run_engines(value, history)
ml = call_ml_inference(...)   # best-effort; None on failure

if any engine == FAIL:                          -> FAIL
elif any REVIEW_REQUIRED or engine discordance: -> REVIEW_REQUIRED
elif legacy PASS and ml.failure_prob_48h >= .8: -> HOLD_PENDING_AI
else:                                           -> PASS

severity = f(status, westgard_rule, analyte_type, anomaly)
```

## 4. Database design rationale

`init.sql` defines the relational schema (Postgres). Highlights:

* **`qc_results`** — the highest-volume table. Indexed on
  `(analyzer_id, test_date)`, `(analyte_code, test_date)` and `qc_status`.
  Documented for conversion to a **TimescaleDB hypertable** or native
  **range partitioning by `test_date`** (quarterly partitions) for retention and
  query locality.
* **Dual control limits** — `control_limits` stores both Westgard
  (`mean ± 3SD`) and QConnect (percentile/KDE) limits per
  analyte/assay/lot/level, plus the `distribution_type` that decides which engine
  is authoritative. `control_limits_history` keeps append-only snapshots for
  drift detection.
* **Capability vs clinical quality** — `westgard_sigma_metrics` holds analytical
  Six Sigma; `diagnostic_sigma_outcomes` holds the consequence-weighted
  diagnostic sigma (sensitivity/specificity/PPV/NPV folded with FN/FP cost
  weights) — critical for qualitative serology.
* **AI lineage** — `ai_predictions` and `distribution_predictions` keep model
  outputs joined to the originating result for explainability and audit.
* **`audit_trail`** — `BIGSERIAL`, **append-only / immutable** (ISO 15189,
  21 CFR Part 11). Production revokes UPDATE/DELETE at the DB-role level.
* UUID PKs via `gen_random_uuid()` (pgcrypto). `gen_random_uuid()` keeps ids
  globally unique across edge/cloud merges. A deferrable FK links
  `qc_results.corrective_action_id` ↔ `capa_actions` so a result and its CAPA can
  be written in one transaction.

## 5. AI layer

* **ml-inference** serves a 48h **failure-probability** predictor and an
  **anomaly** detector. In production these are trained models (gradient-boosted
  trees / isolation forest); the scaffold uses monotone heuristics over the
  point's z-score so the integration is exercised end-to-end.
* **distribution detection** (in qc-evaluation) selects Westgard vs QConnect per
  analyte using Shapiro-Wilk, skewness, kurtosis and Sarle's bimodality
  coefficient — with pure-Python fallbacks when scipy is absent.
* The AI layer is **always best-effort**: if `ml-inference` is unreachable the
  orchestrator still returns a complete, deterministic verdict.

## 6. Federated learning + differential privacy

* Labs never upload raw patient/QC data. Each lab trains locally and submits
  **model updates** over RabbitMQ.
* The `federated-learning` service aggregates updates (FedAvg/FedProx) into a new
  global model version, recorded in `federated_learning_updates` together with
  the **differential-privacy epsilon** (privacy budget) and global metrics.
* DP is applied by clipping per-update gradients and adding calibrated Gaussian
  noise so no single lab's contribution can be reverse-engineered. Lower epsilon
  = stronger privacy (and more noise).

## 7. Security model

* **Transport**: HTTPS everywhere; payloads HMAC-signed (`shared.security`).
* **AuthN**: JWT (HS256) bearer tokens. `REQUIRE_AUTH` toggles enforcement; dev
  defaults to optional so local runs are frictionless.
* **AuthZ**: role hierarchy (`viewer < operator < qc_manager < lab_director <
  admin`) in `cloud/config/security.py`.
* **At rest**: AES-256-GCM helpers for sensitive fields (in `shared.security`).
* **Audit**: every privileged mutation appends to the immutable `audit_trail`.
* **Tracing**: every request carries an `X-Correlation-ID` bound to structured
  loguru logs.

## 8. Compliance notes

* **ISO 15189 / ISO 13485** — full audit trail, control-material lifecycle
  (`qc_materials`), CAPA workflow with effectiveness verification, documented
  control-limit history.
* **21 CFR Part 11** — immutable audit log, attributable actions (user_id), time
  stamps.
* **GDPR** — QC data is largely non-personal, but operator ids are pseudonyms;
  federated learning + DP ensure cross-lab analytics never expose raw records;
  data-retention via TimescaleDB chunk policies.
