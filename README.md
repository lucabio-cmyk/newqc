# QConnect-AI Enterprise

Cloud-native quality-control (QC) monitoring platform for diagnostic laboratories.
QConnect-AI fuses **legacy statistical QC** (Westgard multirules, QConnect
non-Gaussian limits, Six Sigma metrics) with **AI enhancement** (LSTM failure
prediction, anomaly detection, diagnostic sigma) while remaining
**privacy-preserving** (federated learning, differential privacy, GDPR / ISO 15189
aware).

> **Repository layout note.** The original design targets three independent Git
> repositories. This repository hosts all three as a **monorepo** under top-level
> directories so they can be developed together and, if desired, split out later
> with `git subtree split`.

## Projects

| Directory | Role | Stack |
|-----------|------|-------|
| [`qconnect-ai-cloud/`](./qconnect-ai-cloud) | Cloud SaaS backend: QC evaluation, ML inference, federated learning, RCA/CAPA, analytics | Python 3.12, FastAPI, SQLAlchemy (async), PostgreSQL 16, TimescaleDB, Redis, Neo4j, RabbitMQ |
| [`qconnect-ai-edge/`](./qconnect-ai-edge) | Per-lab edge services: HL7 ingestion, offline-capable QC inference, local cache, cloud sync, local dashboard | Python 3.12, FastAPI, SQLite, TensorFlow Lite, Flask |
| [`qconnect-ai-shared/`](./qconnect-ai-shared) | Shared Pydantic models, enums, constants, exceptions, security helpers | Python 3.12, Pydantic v2 |

## Architecture at a glance

```
   ┌──────────────────────────── LAB SITE (edge) ────────────────────────────┐
   │  Analyzer ──HL7/MLLP──▶ qc_inference ──▶ SQLite cache ──▶ data_sync ──┐  │
   │                              │                                        │  │
   │                          web_ui (local dashboard)                     │  │
   └───────────────────────────────────────────────────────────────────── │ ─┘
                                                                            │ mTLS + JWT
                                                                            ▼
   ┌──────────────────────────── CLOUD (SaaS) ───────────────────────────────┐
   │  qc_evaluation ──▶ ml_inference ──▶ rca_capa ──▶ analytics               │
   │        │                │                                                 │
   │   PostgreSQL/Timescale  Redis   Neo4j (RCA graph)   RabbitMQ   federated  │
   └──────────────────────────────────────────────────────────────────────────┘
```

See [`qconnect-ai-cloud/docs/ARCHITECTURE.md`](./qconnect-ai-cloud/docs/ARCHITECTURE.md)
for the full design.

## Quick start

```bash
# Cloud stack
cd qconnect-ai-cloud
cp .env.example .env
docker compose up -d
curl -f http://localhost:8000/health

# Edge stack (per lab)
cd ../qconnect-ai-edge
cp .env.example .env
docker compose up -d
curl -f http://localhost:8000/health
```

## Compliance

The platform is designed with **ISO 15189** (medical laboratory quality) and
**GDPR** (data privacy) in mind: immutable audit trail, RBAC, encryption in
transit (mTLS) and at rest, and differentially-private federated model updates.
This codebase is a reference scaffold and is **not** a certified medical device.

## License

Proprietary — © QConnect-AI. All rights reserved.
