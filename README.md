# homelab-infra

Kubernetes infrastructure for a self-hosted homelab running on a 3-node cluster managed by Proxmox.

## Stack

| Component | Role |
|---|---|
| k3s | Lightweight Kubernetes |
| Traefik v3 | Ingress + TLS termination |
| cert-manager | Wildcard TLS via Cloudflare DNS-01 |
| ArgoCD | GitOps — all changes via Git push |
| Authentik | OIDC/SSO identity provider |
| CloudNativePG | PostgreSQL operator (one cluster per app) |
| Dragonfly | Redis-compatible cache |
| Prometheus + Grafana | Metrics and dashboards |
| Loki + Promtail | Log aggregation |
| CrowdSec | Behavioral IPS (Traefik bouncer) |

## Repository Structure

```
apps/          # ArgoCD Application manifests (App of Apps pattern)
k3s/           # Helm values and raw manifests per service
```

## Secrets

All secrets are SOPS-encrypted with age before being committed. The age public key is in `.sops.yaml`. The private key never touches this repository.

```bash
# Edit an encrypted secret
sops k3s/<service>/secrets.yaml

# Decrypt and apply manually (bootstrap secrets only)
sops -d k3s/<service>/secrets.yaml | kubectl apply -f -
```

## GitOps Flow

Every change goes through Git — no manual `kubectl apply` in production.

```
git push → Gitea → ArgoCD detects drift → applies to cluster
```

ArgoCD is configured with `automated.prune` and `automated.selfHeal` — the cluster always converges to what is in Git.
