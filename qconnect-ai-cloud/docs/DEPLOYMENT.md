# QConnect-AI Cloud — Deployment

## 1. Local deployment (Docker Compose)

The compose file lives at the project root (`qconnect-ai-cloud/`); all build
contexts and bind-mounts are relative to it.

```bash
cd qconnect-ai-cloud
cp .env.example .env            # set real secrets for anything beyond dev
docker compose build            # build all five service images
docker compose up -d            # start the full stack
docker compose ps               # check health
docker compose logs -f qc-evaluation
```

Tear down (keep data):
```bash
docker compose down
```
Tear down (wipe volumes):
```bash
docker compose down -v
```

### Service endpoints once up

| URL | What |
|-----|------|
| http://localhost:8000/docs | qc-evaluation Swagger UI |
| http://localhost:8000/health | qc-evaluation health |
| http://localhost:8001/health | ml-inference |
| http://localhost:8002/health | federated-learning |
| http://localhost:8003/health | rca-capa |
| http://localhost:8004/health | analytics |
| http://localhost:7474 | Neo4j browser |
| http://localhost:15672 | RabbitMQ management UI |

## 2. Environment variables

All configuration is environment-driven (see `.env.example`). The services have
dev-safe defaults, so an empty environment still boots. **Production must
override at minimum**: `JWT_SECRET`, all DB/Neo4j/RabbitMQ passwords,
`NEO4J_AUTH`, and set `REQUIRE_AUTH=true` and `ENVIRONMENT=production`.

## 3. Database initialization

`postgres` runs `cloud/databases/postgres/init.sql` automatically the first time
its data volume is empty (mounted into `/docker-entrypoint-initdb.d/`).

To (re)initialize manually against a running container:
```bash
./scripts/init_db.sh             # waits for postgres, then applies init.sql
```

To seed sample reference data:
```bash
python scripts/seed_data.py      # inserts qc_materials + control_limits (best-effort)
```

> **Extensions**: `init.sql` issues `CREATE EXTENSION` for `pgcrypto`
> (required, ships with contrib), and `timescaledb` / `vector` (only present on
> the TimescaleDB / pgvector images). On vanilla `postgres:16-alpine` the latter
> two will error harmlessly; the schema itself does not depend on them. Use the
> `timescaledb` service (or a pgvector-enabled image) where hypertables /
> embeddings are needed.

## 4. Health probes

Every service exposes `GET /health` (HTTP 200, JSON body with per-dependency
checks). The compose healthchecks `curl /health`. Use the same for orchestration
liveness/readiness probes.

* **Liveness**: `GET /health` returns 200.
* **Readiness**: 200 **and** required `checks` (`db`, `redis`) are `true`.

## 5. Kubernetes notes

Each service maps to a `Deployment` + `Service`; the datastores are best run as
managed offerings or StatefulSets.

```yaml
# qc-evaluation Deployment (excerpt)
spec:
  template:
    spec:
      containers:
        - name: qc-evaluation
          image: registry.example.com/qconnect/qc-evaluation:0.1.0
          ports: [{ containerPort: 8000 }]
          envFrom:
            - secretRef: { name: qconnect-secrets }
            - configMapRef: { name: qconnect-config }
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 20
            periodSeconds: 30
          resources:
            requests: { cpu: "250m", memory: "256Mi" }
            limits:   { cpu: "1",    memory: "1Gi" }
```

* Put secrets (`JWT_SECRET`, DB passwords) in a `Secret`; non-secret config in a
  `ConfigMap`.
* Use a managed Postgres (Cloud SQL / RDS / Aurora) and TimescaleDB Cloud for
  production; run `init.sql` as a one-shot `Job` / migration step.
* Front the services with an Ingress / API gateway terminating TLS and enforcing
  rate limits.
* Scale `qc-evaluation` and `ml-inference` horizontally (stateless). Pin
  `federated-learning` to a single replica per round (leader election otherwise).

## 6. CI/CD

* `.github/workflows/ci.yml` — lint (black/ruff), unit tests against a Postgres
  service, and `docker compose build`.
* `.github/workflows/tests.yml` — unit-test matrix on Python 3.11 and 3.12.
* `.github/workflows/deploy.yml` — builds and pushes images on a version tag
  (registry credentials required; see the workflow comments).

## 7. Observability

* Structured logs via loguru, one correlation id per request (`X-Correlation-ID`).
* `prometheus-client` is bundled; expose a `/metrics` endpoint and scrape per
  service (left as an integration point in this scaffold).
