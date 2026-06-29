# QConnect-AI Cloud — Kubernetes deployment

Kustomize-based manifests for the cloud SaaS backend. Targets Kubernetes
1.28+ (`apps/v1`, `autoscaling/v2`, `networking.k8s.io/v1`).

## What's here

| File | Purpose |
|------|---------|
| `namespace.yaml` | `qconnect-ai` namespace |
| `configmap.yaml` | non-secret config (log level, service URLs, API settings) |
| `secret.example.yaml` | **template** Secret — never applied; create the real one out of band |
| `qc-evaluation.{deployment,service,hpa}.yaml` | API core, 2..10 replicas, `/health` probes |
| `ml-inference.{deployment,service,hpa}.yaml` | inference service, 1..6 replicas, `/health` probes |
| `ingress.yaml` | nginx Ingress for `api.qconnect-ai.example` + TLS |
| `datastores.note.yaml` | comments only — how to run Postgres/Redis/Neo4j/RabbitMQ |
| `kustomization.yaml` | aggregates the appliable resources |

## Prerequisites

- A Kubernetes 1.28+ cluster and `kubectl` (with `kustomize` built in, `kubectl apply -k`).
- An **ingress-nginx** controller for `ingress.yaml`.
- **metrics-server** for the HorizontalPodAutoscalers.
- Datastores provisioned as managed services or operators (see `datastores.note.yaml`).
- A container registry holding the `qc-evaluation` and `ml-inference` images.
- (Recommended) **cert-manager** for automatic TLS, or a pre-created TLS secret.

## 1. Create the real Secret (required)

`secret.example.yaml` is a placeholder template and is **not** part of the
kustomization. Create the real Secret named `qconnect-ai-secrets` before
deploying — ideally via Sealed Secrets, External Secrets Operator, or Vault:

```bash
# Example only — prefer a sealed-secrets/ESO/Vault flow over kubectl create.
kubectl -n qconnect-ai create secret generic qconnect-ai-secrets \
  --from-literal=DATABASE_URL='postgresql+asyncpg://...' \
  --from-literal=TIMESCALE_URL='postgresql+asyncpg://...' \
  --from-literal=REDIS_URL='redis://...' \
  --from-literal=NEO4J_URI='bolt://...' \
  --from-literal=NEO4J_USER='neo4j' \
  --from-literal=NEO4J_PASSWORD='...' \
  --from-literal=RABBITMQ_URL='amqp://...' \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)"
```

Also create the TLS secret `qconnect-ai-tls` (or let cert-manager do it).

## 2. Deploy

```bash
kubectl apply -k deploy/k8s
```

This applies the namespace, config, deployments, services, HPAs and ingress.

## 3. Override the image tag

The `images:` block in `kustomization.yaml` lets you pin a build without
editing the deployment manifests. From CI/CD (see `scripts/deploy.sh`):

```bash
cd deploy/k8s
kustomize edit set image \
  registry.example.com/qconnect-ai/qc-evaluation=REGISTRY/qconnect-ai/qc-evaluation:GITSHA
kustomize edit set image \
  registry.example.com/qconnect-ai/ml-inference=REGISTRY/qconnect-ai/ml-inference:GITSHA
kubectl apply -k .
```

Or render without applying: `kubectl kustomize deploy/k8s`.

## HPA notes

- `qc-evaluation`: 2..10 replicas, scale at 70% average CPU.
- `ml-inference`: 1..6 replicas, scale at 70% average CPU.
- Requires `metrics-server`. Inspect with `kubectl -n qconnect-ai get hpa`.

## Ingress / TLS notes

- Host `api.qconnect-ai.example` → `qc-evaluation` Service (:80 → 8000).
- TLS terminates at the ingress using secret `qconnect-ai-tls`.
- To automate certs, uncomment the `cert-manager.io/cluster-issuer`
  annotation in `ingress.yaml` and create a matching `ClusterIssuer`.

## Verify

```bash
kubectl -n qconnect-ai get pods,svc,hpa,ingress
kubectl -n qconnect-ai port-forward svc/qc-evaluation 8080:80
curl -fsS http://localhost:8080/health
```
