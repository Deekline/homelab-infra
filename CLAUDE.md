# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Migration Plan Reference

Full migration plan: `Homelab_Migration_Plan_v5.docx` (in repo root — binary, extract with `unzip -p Homelab_Migration_Plan_v5.docx word/document.xml | python3 -c "import sys,re; t=re.sub(r'<[^>]+>',' ',sys.stdin.read()); print(re.sub(r'\s+',' ',t))"`)

## Current State (Phase 0 — Bootstrap on Arch PC)

- k3s running on Arch PC (`10.0.50.119`, VLAN50) — **temporary bootstrap node, will be decommissioned**
- Traefik, cert-manager, ArgoCD are deployed and **fully operational**
- sops-secrets-operator deployed and managed by ArgoCD
- All production traffic and persistent data will eventually move to M910Q nodes on VLAN10

**Domain:** `d-kline.org` (wildcard cert `*.d-kline.org`)

## Target Architecture

```
Dell Vostro → Proxmox → OPNsense VM (router/firewall/VLAN/DNS, 10.0.10.1)
Lenovo TS150 → TrueNAS Scale (NFS + Gitea, 10.0.10.150)

M910Q #1/2/3 → Proxmox cluster (corosync)
                └── Terraform (bpg/proxmox) provisions k3s VMs
                    ├── k3s-node-1  10.0.10.10
                    ├── k3s-node-2  10.0.10.11
                    └── k3s-node-3  10.0.10.12
                    └── kube-vip VIP 10.0.10.9  ← always use this in kubeconfig/tools
                └── Monitoring VM 10.0.10.20 (Proxmox HA, Docker Compose — NOT a k3s workload)
```

Traffic: `Internet/LAN → k3s LoadBalancer (80/443) → Traefik → IngressRoute CRDs → Services`

## Planned Repository Structure

This repo (`homelab-infra`) holds k3s manifests. Two sibling repos are planned on Gitea:

```
homelab-infra/        ← this repo — k3s manifests, ArgoCD apps, Helm values
├── .sops.yaml
├── apps/             # ArgoCD Application manifests
└── k3s/
    ├── argocd/
    ├── cert-manager/
    ├── traefik/
    ├── authentik/        # SSO — deploy before any user-facing services
    ├── cloudnativepg/    # Postgres operator — one cluster per app
    ├── dragonfly/        # Redis-compatible cache (replaces Redis in k8s)
    ├── zot/              # Private OCI registry
    └── <app>/            # One directory per service

homelab-ansible/      ← node provisioning (separate Gitea repo)
├── site.yml          # Full node provision — must rebuild from scratch
├── roles/
│   ├── base/         # Ubuntu 24.04, SSH hardening, static IP
│   ├── nfs/          # TrueNAS NFS mounts
│   ├── k3s/          # k3s install (--cluster-init on node 1, --server on others)
│   └── kube-vip/     # Static pod manifest
└── inventory/

homelab-terraform/    ← Proxmox VM provisioning (separate Gitea repo)
├── modules/
│   └── k3s-node/     # Reusable VM module (clone Ubuntu 24.04 cloud-init template)
├── k3s-nodes/
│   └── main.tf       # One module block per node (vm_id, name, ip, cores, memory)
└── monitoring-vm/
```

## Service Deployment Order

Deploy in this exact order (each depends on the previous):

1. ✅ **Traefik** + cert-manager — ingress + TLS
2. ✅ **ArgoCD** + sops-secrets-operator — GitOps controller + cluster-side SOPS decryption
3. ✅ **Root App of Apps** — `kubectl apply -f apps/root.yaml` (one-time; after this all `apps/*.yaml` changes are Git-driven)
4. ✅ **Secrets** — SopsSecret CRDs encrypted in Git, operator decrypting on cluster
5. **NFS StorageClass** — points to TrueNAS `tank_fast` SSD pool
6. **CloudNativePG operator** — one isolated Postgres cluster per app
7. **Dragonfly** — shared Redis-compatible cache
8. **Authentik** — SSO before any user-facing services
9. **Zot** — private OCI registry
10. **Gitea Actions runner** + Trivy — CI pipeline
11. App services (Immich, Paperless, Nextcloud, arr stack, etc.)
12. **Observability** (Prometheus/Grafana/Loki) — in-cluster via kube-prometheus-stack + Loki

## DevSecOps Pipeline Flow

```
Push to Gitea → Gitea Actions → Trivy scan (fail on HIGH/CRITICAL CVEs)
             → push image to Zot → ArgoCD detects change → deploys to k3s
```

No manual `kubectl apply` in production after ArgoCD is configured.

## Secrets Management

Secrets use `kind: SopsSecret` (not `kind: Secret`) — ArgoCD applies the encrypted CRD, the `sops-secrets-operator` decrypts it into a real `kind: Secret` on the cluster. No manual `kubectl apply` needed after the file is encrypted and committed.

```bash
# Always encrypt before committing
sops --encrypt --in-place k3s/<service>/secrets.yaml

# Edit an already-encrypted file (NEVER edit directly — breaks the SOPS MAC)
sops k3s/<service>/secrets.yaml

# Recover from MAC mismatch (caused by direct edit)
sops -d --ignore-mac k3s/<service>/secrets.yaml
```

**Bootstrap secrets** (applied manually once, never in Git):
- `sops-age-key` in `sops-secrets-operator` namespace — the operator's decryption key
- `gitea-homelab-infra` in `argocd` namespace — SSH credentials for Gitea (circular dependency)

age private key: `~/.config/sops/age/keys.txt` — never commit, back up securely.

## Common kubectl/Helm Commands

```bash
# Verify cluster state
kubectl get pods -A
kubectl get certificate -n traefik
kubectl get ingressroute -n traefik
kubectl get middleware -n traefik

# Apply/update manifests
kubectl apply -f k3s/traefik/middlewares.yaml
kubectl apply -f k3s/cert-manager/cert-manager.yaml
kubectl apply -f k3s/argocd/ingress.yaml

# Helm upgrades
helm upgrade traefik traefik/traefik -n traefik -f k3s/traefik/values.yaml
helm upgrade cert-manager jetstack/cert-manager -n cert-manager -f k3s/cert-manager/values.yaml
```

## Key Gotchas

- **k3s ships with Traefik** — must be disabled before Helm install or you get IngressClass conflicts
- **k3s on Arch** — not officially supported; watch for systemd unit or kernel module edge cases
- **k3s cluster-init** — Node 1 must use `--cluster-init` (embedded etcd). Plain install uses SQLite and cannot be expanded to multi-node control-plane
- **kube-vip TLS SAN** — add `10.0.10.9` to `--tls-san` on Node 1 install, before kube-vip exists, to avoid re-issuing certs when adding nodes
- **Always use VIP** (`10.0.10.9`) in kubeconfig and all tools — never individual node IPs
- **cert-manager DNS propagation** — values.yaml sets `--dns01-recursive-nameservers=1.1.1.1:53,8.8.8.8:53` to avoid slow authoritative NS lookups
- **ExternalName doesn't work with IPs** — use headless Service (`clusterIP: None`) + manual Endpoints instead
- **ArgoCD insecure mode** — TLS terminated by Traefik; ArgoCD runs `server.insecure: true`
- **Cloudflare secret in two namespaces** — cert-manager ClusterIssuer looks for secrets in its own namespace AND the Certificate's namespace

## TrueNAS NFS Layout

```
tank_small/ (HDD mirror — large sequential files)
├── immich/        photos + videos
├── paperless/     document archive
├── nextcloud/     user files
├── media/         Plex/Jellyfin library
├── downloads/     qBittorrent
└── backups/       pg_dump, etcd snapshots, configs

tank_fast/ (SSD ~120GB — databases, random I/O)
├── authentik-db/
├── immich-db/     (needs pgvecto.rs extension)
├── paperless-db/
└── nextcloud-db/
```

⚠ One HDD (`sdc`) has SMART failures — replace ASAP with WD Red Plus or Seagate IronWolf (same size or larger).

## Database Strategy

CloudNativePG operator manages all Postgres. Each app gets its own isolated cluster. Dragonfly (Redis-compatible) is the shared cache — more efficient than Redis in k8s environments.

| App | Postgres version | Notes |
|---|---|---|
| Immich | 16 | requires `pgvecto.rs` extension |
| Authentik | 16 | — |
| Paperless | 15 | — |
| Nextcloud | 15 | — |

## Observability

Observability runs **inside k3s** via `kube-prometheus-stack` (Prometheus + Grafana + Alertmanager) + Loki + Promtail DaemonSet. Prometheus TSDB and Loki chunks stored on TrueNAS `tank_fast` via NFS StorageClass.
