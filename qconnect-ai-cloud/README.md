# QConnect-AI Cloud Backend

The **cloud** tier of *QConnect-AI Enterprise* — an AI-augmented Quality Control
(QC) monitoring platform for diagnostic laboratories. It ingests QC results from
edge nodes, evaluates them through deterministic statistical engines (Westgard
multirules, QConnect non-Gaussian limits, Six Sigma) fused with an AI/ML layer
(failure prediction, anomaly detection), manages CAPA workflows, and supports
privacy-preserving federated learning across labs.

This package is one project in the `newqc` monorepo:

```
newqc/
├── qconnect-ai-shared/   # canonical wire contract (Pydantic models, enums)
├── qconnect-ai-edge/     # on-prem edge node (offline-capable evaluation + sync)
└── qconnect-ai-cloud/    # <- THIS project: the cloud backend
```

The cloud project re-declares the wire contract locally (in
`cloud/services/qc_evaluation/schemas.py`) so it can be built as a standalone
Docker image without depending on the shared package being installed.

---

## Services

| Service              | Port | Description |
|----------------------|------|-------------|
| **qc-evaluation**    | 8000 | Orchestration core. Fuses Westgard / QConnect / Sigma / distribution engines with AI enrichment into a single QC verdict. The edge sync target. |
| **ml-inference**     | 8001 | AI enrichment: 48h failure probability, anomaly score, recommended action. |
| **federated-learning** | 8002 | Privacy-preserving cross-lab model aggregation (differential privacy). |
| **rca-capa**         | 8003 | Root-cause analysis over a Neo4j knowledge graph + CAPA workflow. |
| **analytics**        | 8004 | Cross-lab KPI aggregation and reporting. |

### Backing stores

| Component    | Port(s)      | Purpose |
|--------------|--------------|---------|
| postgres     | 5432         | Primary relational store (`init.sql` schema). |
| timescaledb  | 5433 (→5432) | High-volume QC time-series. |
| redis        | 6379         | Control-limit cache / coordination. |
| neo4j        | 7474 / 7687  | RCA failure-knowledge graph (HTTP / Bolt). |
| rabbitmq     | 5672 / 15672 | Federated-learning message bus (AMQP / UI). |

---

## Quick start (Docker Compose)

```bash
cd qconnect-ai-cloud
cp .env.example .env          # adjust secrets for anything beyond local dev
docker compose up -d --build
docker compose ps
```

Then:

```bash
curl http://localhost:8000/health

curl -X POST http://localhost:8000/api/v1/qc/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
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

Interactive API docs are served at `http://localhost:8000/docs`.

---

## Local development (without Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the orchestration service (from the repo root):
uvicorn cloud.services.qc_evaluation.main:app --reload --port 8000

# Run the test suite (engines are pure-python; the API test degrades offline):
pytest cloud/tests/
```

The service starts even without a database or downstream services: persistence
and AI enrichment are best-effort and degrade gracefully.

---

## Environment variables

See [`.env.example`](.env.example) for the full list. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://qconnect:qconnect@postgres:5432/qconnect` | Async Postgres DSN. |
| `TIMESCALE_URL` | `…@timescaledb:5432/qconnect_ts` | Time-series DSN. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis cache. |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://neo4j:7687` / `neo4j` / … | RCA graph. |
| `RABBITMQ_URL` | `amqp://guest:guest@rabbitmq:5672//` | Federated bus. |
| `JWT_SECRET` | `dev-insecure-secret-change-me` | HS256 signing secret — **override in prod**. |
| `REQUIRE_AUTH` | `false` | Enforce bearer auth when `true`. |
| `ML_INFERENCE_URL` | `http://ml-inference:8001` | AI enrichment endpoint. |
| `LOG_LEVEL` | `INFO` | loguru level. |

---

## Repository layout

```
qconnect-ai-cloud/
├── docker-compose.yml          # full stack
├── Dockerfile                  # root image for qc_evaluation (repo-root context)
├── requirements.txt            # umbrella deps (CI)
├── cloud/
│   ├── api/middleware/         # reusable correlation-id + audit middleware
│   ├── config/                 # settings, async DB engine, security/RBAC
│   ├── databases/postgres/     # init.sql (full schema)
│   ├── services/
│   │   ├── qc_evaluation/      # orchestration core + engines + schemas
│   │   ├── ml_inference/       # AI enrichment (stub)
│   │   ├── federated_learning/ # FL coordinator (stub)
│   │   ├── rca_capa/           # RCA/CAPA (stub)
│   │   └── analytics/          # KPI reporting (stub)
│   └── tests/                  # cross-cutting tests
├── docs/                       # ARCHITECTURE / API_REFERENCE / DEPLOYMENT
└── scripts/                    # init_db.sh, seed_data.py
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) and
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for details.

---

## License

Proprietary — © QConnect-AI.
