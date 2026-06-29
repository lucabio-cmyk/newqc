# QConnect-AI Edge — production deployment

Provision and run the per-lab edge stack (`qc-inference`, `data-sync`,
`web-ui`) on a single lab node, managed by systemd and Docker Compose.

## What's here

| File | Purpose |
|------|---------|
| `docker-compose.prod.yml` | production override (limits, log rotation, read-only, healthchecks) |
| `systemd/qconnect-edge.service` | systemd unit running the compose stack |
| `install.sh` | idempotent provisioner / `--uninstall` |

The production override is applied **on top of** the base
`docker-compose.yml` at the edge repo root:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d
```

## Provision a new lab node

On the lab machine (Linux + systemd + Docker Engine with the compose v2
plugin), from a checkout of `qconnect-ai-edge`:

```bash
sudo ./deploy/install.sh
```

This will:

1. Verify `docker` and `docker compose`.
2. Sync the project into `/opt/qconnect-ai-edge` (override `INSTALL_DIR=`).
3. Create `/opt/qconnect-ai-edge/.env` from `.env.example` if missing
   (mode `600`) and tell you to edit it.
4. Install, enable and start `qconnect-edge.service`.

Re-running is safe: an existing `.env` is never overwritten, and the unit
is restarted with the latest files.

### Uninstall

```bash
sudo ./deploy/install.sh --uninstall
```

Stops and removes the unit. The data volume and `/opt/qconnect-ai-edge`
are intentionally left in place; the script prints the commands to wipe
them if you really want to.

## Configure `.env`

Edit `/opt/qconnect-ai-edge/.env` (template: `.env.example`). Key fields:

- `LAB_ID` — unique lab identifier, e.g. `lab-genova-001`.
- `LAB_NAME` — human-readable name shown in the dashboard.
- `CLOUD_URL` — base URL of the cloud API, **no trailing slash**.
- `LAB_AUTH_TOKEN` — per-lab JWT sent as `Authorization: Bearer <token>`.
- `SYNC_INTERVAL` — seconds between cloud sync cycles.
- `ENABLE_HL7` / `HL7_LISTEN_PORT` — analyzer HL7/MLLP ingestion.

After any change: `sudo systemctl restart qconnect-edge.service`.

## TLS to the cloud

- Always set `CLOUD_URL` to an `https://` endpoint; the sync daemon uses
  the system CA bundle to verify the cloud certificate.
- For a private/enterprise CA, install the CA cert into the host trust
  store (e.g. `/usr/local/share/ca-certificates/` + `update-ca-certificates`)
  so containers built on a base image that trusts the host bundle can
  validate it. Do **not** disable certificate verification.
- Keep `LAB_AUTH_TOKEN` secret; `.env` is created mode `600`.

## Update procedure

1. Pull the new code on the node (or re-checkout).
2. Re-run `sudo ./deploy/install.sh` (re-syncs files, restarts the unit).
   The unit also runs `docker compose pull` on start to fetch newer images.
3. Verify: `systemctl status qconnect-edge.service` and the healthchecks
   (`docker compose ps`).

## Logs & troubleshooting

```bash
# unit-level (start/stop, compose output)
journalctl -u qconnect-edge.service -f

# per-service container logs (rotated: 10m x 5 via json-file)
cd /opt/qconnect-ai-edge
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs -f qc-inference

# local health probes
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/status
```

Common issues:

- **Service keeps restarting** — check `.env` (missing `LAB_ID`/`CLOUD_URL`)
  and `journalctl -u qconnect-edge.service`.
- **No cloud sync** — confirm `CLOUD_URL` reachability and a valid
  `LAB_AUTH_TOKEN`; the daemon retries with backoff and stays "healthy"
  while offline by design.
- **Permission/read-only errors** — services run read-only with a `/tmp`
  tmpfs; persistent state must go to the `edge_data` volume (`/data`).
