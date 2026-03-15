# Homelab Migration Plan: Arch PC → k3s on Proxmox

## Current State (March 2026)

| Component | Status | Notes |
|---|---|---|
| k3s | Running | Arch PC `10.0.50.119` (VLAN50) — bootstrap only |
| SOPS operator | Running | age key configured |
| cert-manager | Running | Cloudflare DNS challenge |
| Traefik | Running | LoadBalancer on `10.0.50.119` |
| Wildcard cert | Ready | `*.d-kline.org` via Let's Encrypt |
| Gitea | Running | TrueNAS app, `http://10.0.10.150:30008` |
| ArgoCD | Installing | In progress |
| CloudNativePG | Pending | After ArgoCD |
| Dragonfly | Pending | After CloudNativePG |
| Authentik | Pending | After databases |
| Zot registry | Pending | After ArgoCD |
| Gitea Actions runner | Pending | After Zot |
| Monitoring VM | Pending | After M910Q cluster exists |

---

## Phase 0 — Finish on Arch PC

Goal: build and test the entire stack on the Arch PC before spending any money on hardware. Every mistake here costs nothing; on M910Q it costs time.

### Remaining Deployment Order

Deploy in this exact order — each depends on the previous:

1. **ArgoCD** — all subsequent deploys go through Git after this point
2. **Commit all current manifests** to Gitea `homelab-infra` repo, configure ArgoCD app
3. **NFS StorageClass** in k3s pointing to TrueNAS `tank_fast`
4. **CloudNativePG operator** — one Postgres cluster per app
5. **Dragonfly** — shared Redis-compatible cache
6. **Authentik** — SSO before any user-facing services
7. **Zot** — private OCI registry
8. **Gitea Actions runner** — CI pipeline
9. **Trivy** — vulnerability scanning in CI
10. App services (Immich, Paperless, Nextcloud, arr stack)

> Monitoring VM (Prometheus + Grafana + Loki) is provisioned **after** M910Q cluster, not on k3s.

### Phase 0 Completion Checklist

- [ ] ArgoCD deployed, syncing from Gitea `homelab-infra`
- [ ] All manifests committed — ArgoCD is the only deploy path
- [ ] Authentik configured with SSO working for at least one service
- [ ] Full pipeline: Gitea push → Trivy scan → Zot push → ArgoCD deploy
- [ ] Ansible `site.yml` written, tested targeting localhost, committed to Gitea
- [ ] All services migrated from Docker Compose to k3s (Phase 3 pattern)
- [ ] Grafana dashboards showing all services healthy

---

## Gitea Repository Structure

Three separate repos on TrueNAS Gitea (`http://10.0.10.150:30008`):

```
homelab-infra/         ← k3s manifests, ArgoCD apps, Helm values
├── .sops.yaml
├── apps/              ArgoCD Application manifests
└── k3s/
    ├── argocd/
    ├── cert-manager/
    ├── traefik/
    ├── authentik/
    ├── cloudnativepg/
    ├── dragonfly/
    ├── zot/
    └── <app>/

homelab-ansible/       ← node provisioning (Arch PC → NFS mounts → k3s install)
├── site.yml           Full node provision from scratch
├── roles/
│   ├── base/          Ubuntu 24.04, SSH hardening, static IP
│   ├── nfs/           TrueNAS NFS mounts
│   ├── k3s/           k3s install (--cluster-init on node 1, --server on others)
│   └── kube-vip/      Static pod manifest
└── inventory/

homelab-terraform/     ← Proxmox VM provisioning
├── modules/k3s-node/  Reusable VM module (clone Ubuntu 24.04 cloud-init template)
├── k3s-nodes/         One module block per node
└── monitoring-vm/
```

**Ansible requirement:** the playbook must be able to fully rebuild any node from scratch. This is the single most important operational requirement.

---

## DevSecOps Pipeline

```
Push to Gitea (homelab-infra or app repo)
  │
  ▼
Gitea Actions runner (k3s pod)
  ├── Build container image
  ├── Trivy scan → fail on HIGH/CRITICAL CVEs
  └── Push image to Zot (private registry, on k3s)
        │
        ▼
      ArgoCD detects manifest change in Git
        │
        ▼
      Deploys to k3s automatically
        │
        ▼
      Prometheus + Grafana + Loki observe the result
```

**No manual `kubectl apply` in production after ArgoCD is configured.**

### Secrets

All `*secrets.yaml` files are SOPS-encrypted (`data`/`stringData` fields only).

```bash
# Edit a secret
sops k3s/<service>/secrets.yaml

# Decrypt and apply
sops -d k3s/<service>/secrets.yaml | kubectl apply -f -
```

age private key: `~/.config/sops/age/keys.txt` — never commit, back up offline.

---

## Phase 1 — M910Q Arrives: Foundation

### Hardware per node
- Lenovo M910Q Tiny
- i5-7500T or i5-6700T (equivalent for this workload)
- 16 GB DDR4
- 256 GB SSD

### Proxmox Setup (bare metal on each M910Q)

1. Install Proxmox VE — static IP on VLAN10, SSH key access
2. Build Ubuntu 24.04 cloud-init template using Packer, push to Proxmox as base image
3. Join all three M910Q nodes into a single **Proxmox cluster** (corosync)
   - All on same network (VLAN10 local switch — low latency required)
   - Must be odd number for Proxmox quorum ✓

### Terraform Provisioning (bpg/proxmox provider)

Terraform targets the Proxmox cluster API as a single endpoint. Each k3s VM clones the Packer-built Ubuntu 24.04 template.

```hcl
# homelab-terraform/k3s-nodes/main.tf
module "k3s_node_1" {
  source   = "./modules/k3s-node"
  vm_id    = 101
  name     = "k3s-node-1"
  ip       = "10.0.10.10"
  cores    = 4
  memory   = 15360
  template = "ubuntu-2404-cloud-init"
}

module "monitoring_vm" {
  source   = "./modules/k3s-node"
  vm_id    = 120
  name     = "monitoring"
  ip       = "10.0.10.20"
  cores    = 2
  memory   = 4096
  template = "ubuntu-2404-cloud-init"
}
```

Terraform state stored in Gitea `homelab-terraform` repo — no external dependencies.

**Validation:** run `terraform destroy && terraform apply` once to confirm full rebuild works.

### Ansible Provisioning (targets the VM, not bare metal)

```bash
# Provision first node
ansible-playbook -i 10.0.10.10, site.yml

# Test playbook locally first (on Arch PC)
ansible-playbook -i localhost, -c local site.yml
```

Playbook handles: Ubuntu 24.04 base, SSH hardening, static IP, NFS mounts from TrueNAS, k3s install, kube-vip.

### Phase 1 Validation Checklist

- [ ] Proxmox VE installed and accessible on each M910Q
- [ ] Packer template built and pushed to Proxmox
- [ ] Terraform provisions `k3s-node-1` VM (10.0.10.10) and monitoring VM (10.0.10.20)
- [ ] Ansible provisions VM — NFS mounted, k3s running with `--cluster-init`
- [ ] kube-vip VIP reachable at `10.0.10.9`
- [ ] `terraform destroy && apply` completes cleanly
- [ ] Gitea accessible — all three repos exist and have content

---

## Phase 2 — k3s Cluster Setup (Single Node First)

### Node 1: cluster-init mode

```bash
# --cluster-init enables embedded etcd (not SQLite)
# Never use plain install — it cannot be expanded to multi-node control-plane
curl -sfL https://get.k3s.io | sh -s - \
  --cluster-init \
  --tls-san 10.0.10.9 \   # VIP — add before kube-vip exists
  --tls-san 10.0.10.10
```

### kube-vip (deploy before adding Node 2)

```bash
docker run --rm ghcr.io/kube-vip/kube-vip:latest manifest pod \
  --interface eth0 --address 10.0.10.9 \
  --controlplane --arp --leaderElection \
  | sudo tee /var/lib/rancher/k3s/agent/pod-manifests/kube-vip.yaml

# Verify
kubectl --server https://10.0.10.9:6443 get nodes
```

**Always use VIP `10.0.10.9` in kubeconfig, ArgoCD, and Ansible — never individual node IPs.**

### Infrastructure Services Deployment Order

Same order as Phase 0, now against the real node:

1. **Traefik + cert-manager** — validate SSL end-to-end with a `whoami` pod before continuing
2. **Authentik** — SSO before any user-facing services
3. **CloudNativePG** — one Postgres cluster per app, on TrueNAS `tank_fast` NFS
4. **Dragonfly** — shared Redis-compatible cache
5. **Zot** — private OCI registry
6. **Gitea Actions runner** + **Trivy** — CI pipeline active
7. **ArgoCD** — point at Gitea `homelab-infra`; everything deploys via Git from here
8. Application services

---

## Phase 3 — Service Migration (One at a Time)

**DNS cutover trick:** OPNsense split-horizon DNS means cutover = change one Unbound record from old PC IP to Traefik IP. Zero router changes, instant rollback.

### Migration Pattern

```
1. Stop writes on old container (maintenance mode if available)
2. pg_dump / export data from old container
3. Transfer data to TrueNAS NFS share
4. Deploy new instance on k3s → TrueNAS NFS + centralized Postgres
5. Verify on temporary internal URL
6. Update OPNsense Unbound record → k3s Traefik IP
7. Confirm, then decommission old Docker container
```

### Migration Order

| Service | Migration Effort | Notes |
|---|---|---|
| Immich | High | pg_dump + photo files to NFS; thumbnails rebuild |
| Paperless NGX | High | pg_dump + document archive; verify search index |
| Nextcloud | High | Files to NFS + DB dump; test mobile sync |
| Jellyfin | Medium | Media already on TrueNAS; config + metadata only |
| Sonarr / Radarr | Medium | Config + DB; re-link media paths |
| Prowlarr | Low | Config only |
| qBittorrent | Low | Pause downloads before migrate |
| Overseerr | Low | Reconnect to Sonarr/Radarr after |
| Homepage | Low | Reconfigure service links |

---

## Phase 4 — Cluster Expansion (Nodes 2 + 3)

Start expansion only after services are stable for 2–4 weeks on Node 1.

### Adding Node 2

```bash
# Get join token from Node 1
cat /var/lib/rancher/k3s/server/node-token

# Join Node 2 — --server points to VIP, not Node 1 IP
curl -sfL https://get.k3s.io | sh -s - \
  --server https://10.0.10.9:6443 \
  --token <token>

# Copy kube-vip manifest to Node 2 (same file, same path)
```

> ⚠ With 2 control-plane nodes, etcd has **no quorum tolerance** (needs 2/2). Add Node 3 quickly.

### Adding Node 3 (True HA)

Same process as Node 2. After Node 3 joins: etcd tolerates 1 node failure, VIP floats across all 3 nodes.

```bash
# Validate etcd health
kubectl get nodes
k3s etcd-snapshot ls

# Test HA: power off Node 1, verify kubectl still works via VIP
```

---

## Monitoring VM (After M910Q Cluster)

Runs on dedicated Proxmox HA VM (`10.0.10.20`) — **not** a k3s workload. Observer independence: if k3s fails, Grafana shows you why.

- Provisioned by Terraform + Ansible (Docker Compose inside VM)
- Proxmox HA auto-restarts on node failure
- Data on TrueNAS `tank_fast/monitoring` — survives VM restarts

**Stack:** Prometheus, Grafana, Loki, Alertmanager

**Scrape targets:** k3s nodes (node-exporter, kube-state-metrics, Traefik, ArgoCD, CloudNativePG), Proxmox hosts, TrueNAS, OPNsense

**Logs:** Promtail DaemonSet in k3s ships pod logs → Loki on monitoring VM

---

## IP / Network Reference

| Host | IP | Role |
|---|---|---|
| OPNsense | 10.0.10.1 | Gateway, DNS, firewall |
| TrueNAS TS150 | 10.0.10.150 | NFS + Gitea |
| Arch PC (current) | 10.0.50.119 | k3s bootstrap — decommission after migration |
| M910Q Node 1 | 10.0.10.10 | Proxmox + k3s VM |
| M910Q Node 2 | 10.0.10.11 | Proxmox + k3s VM |
| M910Q Node 3 | 10.0.10.12 | Proxmox + k3s VM |
| kube-vip VIP | 10.0.10.9 | k3s API server — use this everywhere |
| Monitoring VM | 10.0.10.20 | Prometheus + Grafana + Loki |

## TrueNAS NFS Layout

```
tank_small/  (HDD mirror — large sequential files)
├── immich/        photos + videos
├── paperless/     document archive
├── nextcloud/     user files
├── media/         Jellyfin library
├── downloads/     qBittorrent
└── backups/       pg_dump, etcd snapshots, configs

tank_fast/   (SSD ~120GB — databases + random I/O)
├── authentik-db/
├── immich-db/     (requires pgvecto.rs extension, Postgres 16)
├── paperless-db/
├── nextcloud-db/
└── monitoring/    Prometheus TSDB + Loki chunks
```

⚠ `sdc` has 13 SMART failures at LBA 1195388728. Pool is Online via mirror but replace ASAP (WD Red Plus or Seagate IronWolf, same size or larger).

## Database Reference (CloudNativePG)

| App | Postgres version | Special |
|---|---|---|
| Immich | 16 | `pgvecto.rs` extension required |
| Authentik | 16 | — |
| Paperless NGX | 15 | — |
| Nextcloud | 15 | — |

Dragonfly (Redis-compatible) as shared cache for all apps.

---

## Key Design Principles

- **Git is the source of truth** — if it's not in Git, it doesn't exist
- **Ansible can rebuild any node from scratch** — the playbook is the documentation
- **ArgoCD deploys everything** — no manual `kubectl apply` in production
- **TrueNAS holds all persistent data** — k3s nodes are stateless and disposable
- **One service migrated at a time** — DNS cutover per service, instant rollback
- **Observe before you act** — monitoring VM deployed before application services
- **Start simple, expand later** — single node stable before adding Node 2
