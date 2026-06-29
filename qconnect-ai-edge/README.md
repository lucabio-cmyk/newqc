# QConnect-AI Edge

Per-laboratory **edge node** for the QConnect-AI Enterprise QC monitoring
platform. The edge runs *inside the lab* and is designed to keep working when the
network to the cloud is down: it evaluates every quality-control (QC) result
locally, caches it durably, and syncs to the cloud opportunistically.

This package contains the EDGE services only. The central platform lives in the
sibling `qconnect-ai-cloud` package; the shared wire contract lives in
`qconnect-ai-shared`. To stay independently buildable in Docker, the edge
**re-declares** the relevant schemas locally in
`edge/qc_inference/schemas.py` (kept consistent with `shared.models`).

## Role of the edge node

* **Offline-first QC evaluation.** Westgard multirules, QConnect percentile
  limits, an LSTM-lite 48h failure forecast and statistical anomaly detection
  all run locally, fused into a single decision within a **<100ms** budget.
  Nothing here requires the cloud, numpy, or TensorFlow at runtime.
* **HL7 ingestion.** An optional async MLLP listener accepts HL7 v2.5 results
  pushed by analyzers, ACKs them, and feeds them into evaluation.
* **Durable local cache.** A stdlib SQLite database stores pending results,
  cached control limits and model blobs, so an outage never loses data.
* **Cloud sync.** A separate daemon batches pending results to the cloud, pulls
  down updated models and control limits, and retries with exponential backoff.
* **Local dashboards.** A minimal built-in dashboard ships with the inference
  service; a richer Flask operator dashboard (Levey-Jennings charts, per-analyte
  pass/fail, sync status) runs alongside. Both are fully self-contained with
  **no external CDNs**, so they work air-gapped.

## Graceful degradation

Every code path that touches the network or optional ML degrades cleanly:

| Condition                       | Behaviour                                              |
| ------------------------------- | ----------------------------------------------------- |
| Cloud unreachable               | Results cached locally; sync retries with backoff     |
| No TFLite/TensorFlow runtime    | LSTM-lite uses a deterministic heuristic forecast      |
| Control limits stale/missing    | QConnect derives percentiles from local history        |
| ML/anomaly step errors          | Falls back to Westgard-only; still returns a decision  |

## Services & ports

| Service        | Container         | Port  | Purpose                                        |
| -------------- | ----------------- | ----- | ---------------------------------------------- |
| `qc-inference` | FastAPI + uvicorn | 8000  | `POST /evaluate`, `/health`, `/status`, dashboard |
| `qc-inference` | HL7/MLLP listener | 2575  | Inbound HL7 v2.5 (active when `ENABLE_HL7=true`)  |
| `data-sync`    | asyncio daemon    | —     | Cloud upload + model/limit pull (egress only)  |
| `web-ui`       | Flask             | 8001  | Operator dashboard (LJ charts, sync status)    |

### Key HTTP endpoints (qc-inference, port 8000)

* `POST /evaluate` — evaluate a `QCDataInput`, returns a `QCEvaluationResponse`.
* `GET /` — minimal built-in dashboard (last 24h summary, pending, sync state).
* `GET /health` — DB / model / cloud checks (always 200; body shows status).
* `GET /status` — sync status JSON (pending uploads, last sync, models loaded).
* `POST /sync/trigger` — surface pending count (upload is done by `data-sync`).

## Environment variables

Copy `.env.example` to `.env` and adjust. `docker compose` reads it via `env_file`.

| Variable             | Default                              | Description                                  |
| -------------------- | ------------------------------------ | -------------------------------------------- |
| `LAB_ID`             | `lab-unknown`                        | Lab identifier (matches the cloud tenant)    |
| `LAB_NAME`           | `Unnamed Laboratory`                 | Display name for dashboards                   |
| `CLOUD_URL`          | *(empty)*                            | Base URL of the cloud API                     |
| `LAB_AUTH_TOKEN`     | *(empty)*                            | Per-lab JWT (`Authorization: Bearer`)         |
| `ANALYZER_HOST`      | `10.20.0.11`                         | Primary analyzer host (informational/config)  |
| `ANALYZER_PORT`      | `2575`                               | Primary analyzer port                          |
| `HL7_LISTEN_PORT`    | `2575`                               | MLLP listen port                               |
| `ENABLE_HL7`         | `false`                              | Start the HL7/MLLP listener in qc-inference   |
| `SYNC_INTERVAL`      | `300`                                | Seconds between cloud sync cycles             |
| `RETRY_MAX_ATTEMPTS` | `10`                                 | Backoff attempts per network operation        |
| `DB_PATH`            | `/data/qc_cache.db`                  | SQLite cache path (inside the container)       |
| `LOG_LEVEL`          | `INFO`                               | `DEBUG` / `INFO` / `WARNING` / `ERROR`        |

## Running with Docker Compose

```bash
cd qconnect-ai-edge
cp .env.example .env          # then edit LAB_ID, CLOUD_URL, LAB_AUTH_TOKEN
docker compose up --build
```

* Inference API + built-in dashboard: <http://localhost:8000>
* Operator dashboard: <http://localhost:8001>

The three services share a named volume (`edge_data`) holding the SQLite cache
and cached models, so `data-sync` and `web-ui` see exactly what `qc-inference`
writes.

### Example evaluation request

```bash
curl -s http://localhost:8000/evaluate -H 'content-type: application/json' -d '{
  "lab_id": "lab-genova-001",
  "analyzer_id": "ABBOTT-ARCHITECT-001",
  "analyte_code": "HCV-AB",
  "analyte_type": "serology",
  "qc_lot_id": "QC-HCV-DIAMEX-202603-001",
  "qc_level": "NORMAL",
  "result_value": 1.45,
  "target_value": 1.50,
  "sd_value": 0.08,
  "operator_id": "EMP00234"
}'
```

## Local development

The engines, cache and HL7 parser are pure standard library (plus Pydantic for
schemas), so the test suite runs without FastAPI/TensorFlow/numpy:

```bash
pip install pydantic pytest
pytest tests/test_hl7_parsing.py tests/test_inference.py
```

## Layout

```
qconnect-ai-edge/
├── docker-compose.yml          # 3-service edge stack
├── Dockerfile                  # convenience build for qc-inference
├── requirements.txt            # dependency-light (ML optional)
├── .env.example
├── data/                       # runtime cache + models (git-ignored)
├── edge/
│   ├── qc_inference/           # FastAPI app, engines, cache, HL7
│   │   ├── main.py
│   │   ├── schemas.py
│   │   ├── models/             # westgard, qconnect, lstm_lite, anomaly
│   │   ├── hl7/                # listener, parser, validator
│   │   └── cache/              # sqlite_manager, model_cache
│   ├── data_sync/              # uploader, retry_logic, queue_manager, run
│   ├── web_ui/                 # Flask dashboard (inline JS LJ chart)
│   └── config/                 # lab_config.yaml, cloud_connection.yaml
└── tests/                      # pure-stdlib + pydantic tests
```

## Security notes

* Cloud requests authenticate with a per-lab JWT (`Authorization: Bearer`).
* TLS verification is **on** by default; configure a CA bundle (mTLS) via
  `cloud_connection.yaml` / `CLOUD_CA_BUNDLE` rather than disabling it.
* The `.env` file (secrets) is git-ignored; never commit real tokens.
