# homelab-infra

Kubernetes infrastructure for a self-hosted homelab running on a 3-node cluster managed by Proxmox.

## Stack

| Component | Role |
|---|---|
| k3s | Lightweight Kubernetes |
| Cilium | CNI |
| Traefik v3 | Ingress + TLS termination |
| cert-manager | Wildcard TLS via Cloudflare DNS-01 |
| Flux CD | GitOps — all changes via Git push |
| Authelia | Forward-auth SSO (replaced Authentik, kept decommissioned in `apps/authentik.yaml.disabled`) |
| CloudNativePG | PostgreSQL operator (one cluster per app) |
| Dragonfly | Redis-compatible cache |
| VictoriaMetrics stack | Metrics, Grafana dashboards, alerting (replaced Prometheus + Grafana) |
| Loki + Promtail | Log aggregation |
| CrowdSec | Behavioral IPS (Traefik bouncer) |
| Apprise | Notification gateway (CI failures, alerts) |

## Repository Structure

```
apps/          # Flux Kustomization/HelmRelease manifests
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

Every change goes through Git — no manual `kubectl apply` in production. Flux watches the `deploy` branch, **not** `master`.

```
git push (master) → Gitea Actions CI validates → CI force-pushes to deploy → Flux → cluster
```

CI (`.gitea/workflows/validate.yaml`) runs on every push/PR to `master`:

| Job | Checks |
|---|---|
| `sops-check` | Fails if any `k3s/**/secrets.yaml` is unencrypted |
| `gitleaks` | Secret scanning |
| `kubeconform` | Validates rendered Kubernetes manifests |
| `trivy` | Config misconfiguration scan |
| `promote` | On success, merges the commit into `deploy` and pushes |

Never push directly to `deploy` — only the `promote` job does that. Flux's `Kustomization`/`HelmRelease` objects reconcile on a fixed interval with pruning enabled, so the cluster always converges to `deploy` branch state.

## Dependency Updates

Renovate (`.gitea/workflows/renovate.yaml`) runs on a schedule and opens PRs for chart/image updates against `master`. Each Renovate PR gets an automated risk review (`.gitea/workflows/ai-review.yaml`) that renders the chart version diff and asks Claude to assess it, posting the result as a PR comment.
