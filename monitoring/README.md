# QConnect-AI Monitoring Stack

Prometheus + Grafana + node-exporter for observing the QConnect-AI cloud and
edge services. Both services expose a Prometheus-format `/metrics` endpoint.

## Layout

```
monitoring/
├── docker-compose.monitoring.yml      # Prometheus + Grafana + node-exporter
├── prometheus/
│   ├── prometheus.yml                 # scrape config (15s) + rule_files
│   └── alerts/rules.yml               # alerting rules
└── grafana/
    ├── provisioning/
    │   ├── datasources/datasource.yml # Prometheus datasource (default)
    │   └── dashboards/dashboards.yml  # dashboard provider
    └── dashboards/
        └── qconnect-overview.json     # the overview dashboard
```

## Running

```bash
docker compose -f monitoring/docker-compose.monitoring.yml up -d
```

- Prometheus: http://localhost:9090
- Grafana:    http://localhost:3000
- node-exporter: http://localhost:9100/metrics

## Default credentials

Grafana logs in as `admin` / `admin` by default. Override the password with the
`GRAFANA_ADMIN_PASSWORD` environment variable before bringing the stack up:

```bash
GRAFANA_ADMIN_PASSWORD='a-strong-secret' \
  docker compose -f monitoring/docker-compose.monitoring.yml up -d
```

Self-signup is disabled (`GF_USERS_ALLOW_SIGN_UP=false`).

## Connecting to the cloud / edge services

Prometheus scrapes the targets `qc-evaluation:8000` and `qc-inference:8000`
(see `prometheus/prometheus.yml`). For those DNS names to resolve, Prometheus
must share a Docker network with each service. The cloud and edge compose
stacks each create a network; declare it as an **external** network in
`docker-compose.monitoring.yml` and attach the `prometheus` service to it. There
is a worked example in the header comment of that compose file. The dedicated
`qconnect-monitoring` network is used for Prometheus <-> Grafana traffic.

For host-based deployments (no Docker DNS), change the targets in
`prometheus.yml` to the reachable host:port of each service.

## Dashboards

`grafana/dashboards/qconnect-overview.json` is auto-provisioned into the
"QConnect-AI" folder. Panels: QC evaluation throughput, PASS vs FAIL rate,
p95 evaluation latency, ML inference availability, edge pending uploads, and
HTTP request rate by status.

## Alerts

Alerting rules live in `prometheus/alerts/rules.yml`:

- `HighQCFailureRate` (warning) — FAIL ratio > 20% for 10m
- `QCEvaluationLatencyHigh` (warning) — p95 latency > 1s for 10m
- `MLInferenceDown` (warning) — `qconnect_ml_inference_up == 0` for 5m
- `EdgeBacklogGrowing` (warning) — `qconnect_edge_pending_uploads > 500` for 15m
- `CloudServiceDown` / `EdgeServiceDown` (critical) — `up == 0` for 2m

Alertmanager wiring is left commented in `prometheus.yml`; enable it once an
Alertmanager instance is deployed.

## Reloading config without a restart

The Prometheus container runs with `--web.enable-lifecycle`, so configuration
and rule changes can be applied live:

```bash
curl -XPOST http://localhost:9090/-/reload
```
