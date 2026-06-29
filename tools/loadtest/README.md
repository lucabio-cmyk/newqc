# Load & smoke test harness

A dependency-light (httpx + stdlib) client for a **running** QConnect-AI cloud
stack. Start the stack first (`cd qconnect-ai-cloud && docker compose up -d`),
then:

```bash
pip install httpx                      # only runtime dependency

# Fast end-to-end sanity check (CI gate). Exits non-zero on any failure.
python tools/loadtest/qc_load.py smoke --base-url http://localhost:8000

# Closed-loop load: 20 workers for 30s, reports throughput + latency p50/p95/p99.
python tools/loadtest/qc_load.py load --base-url http://localhost:8000 \
    --concurrency 20 --duration 30
```

`load` exits non-zero if the error rate exceeds 1%, so it doubles as a
performance regression gate.

## What `smoke` checks

- `GET /health` is 200 and identifies the `qc-evaluation` service
- `POST /api/v1/qc/evaluate` returns a valid fused verdict with the
  `westgard` / `qconnect` / `sigma` legacy fragments
- a forced out-of-control value is rejected (`FAIL`)
- `POST /api/v1/labs/{lab_id}/qc/batch` accepts a batch
- `GET /metrics` exposes the Prometheus counters

## Data generation

`build_payload(seed, index)` produces deterministic QCDataInput payloads; ~1 in
11 is a gross outlier so the FAIL path is exercised under load. The payload
builder and latency summariser are pure functions covered by `test_loadtest.py`:

```bash
pytest tools/loadtest -q
```

## Locust (optional)

`locustfile.py` offers an interactive profile (ramping, charts) when you install
locust separately — see the file header.
```
