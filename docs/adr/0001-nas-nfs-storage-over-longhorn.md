# 1. Use NAS-backed NFS for stateful storage instead of Longhorn

## Status

Accepted (reflects the storage architecture already in place across `k3s/nfs`, `k3s/*/pvc.yaml`, and every CloudNativePG `Cluster` manifest).

## Context

The cluster is 3 Proxmox VMs (`k3s-cp-1`, `k3s-worker-1`, `k3s-worker-2`), each provisioned via Terraform (`homelab-provisioning/terraform/modules/k3s-vm`) with a single `local-lvm` (node-local, non-shared) disk and no dedicated extra block device for distributed storage use. The template module even flags this explicitly:

> `local-lvm is node-local storage. The Ubuntu 24.04 cloud-init template must exist on every Proxmox node before running terraform apply.`

Stateful workloads on the cluster fall into two very different shapes:

- **Small, latency-sensitive state**: app configs and CloudNativePG PostgreSQL data volumes (`immich-db`, `authentik`, `paperless`, `authelia`), Prometheus/VictoriaMetrics/Loki data, Sonarr/Radarr/Prowlarr/Bazarr/qBittorrent/Gotify/CrowdSec/Homarr configs — typically 1–10Gi each, `ReadWriteOnce`.
- **Large, shared bulk data**: Immich photo library (2Ti), the `*arr`/Jellyfin/qBittorrent media library (1Ti), Paperless document store (100Gi), Audiobookshelf library (500Gi) — all `ReadWriteMany`, since multiple pods (Sonarr, Radarr, qBittorrent, Jellyfin, Immich, Immich Power Tools) need concurrent access to the same files.

The cluster is also intentionally powered down and woken back up on a schedule (see `homelab-cluster-agent`) to save power — pods get rescheduled across whichever nodes come back up first, not pinned to one node.

We needed a storage backend that works for both shapes without adding significant operational or resource overhead on top of already resource-constrained VMs (observed firsthand: VictoriaMetrics OOMKilling at a 512Mi limit, qBittorrent needing memory tuning — headroom on these nodes is not generous).

## Decision

Use the existing NAS (ZFS-based, two pools) as the storage backend for all stateful Kubernetes workloads, mounted via `csi-driver-nfs` (NFSv4.1):

- **`SSD_SMALL` pool** — small, latency-sensitive data. Exposed as three dynamically-provisioned `StorageClass`es (`nfs-storage`, `nfs-db`, `nfs-monitoring`), `reclaimPolicy: Retain`, mount options tuned for the workload (`hard`, `noatime`, `nodiratime`, `nconnect=8`).
- **`tank_small` pool** — bulk media/document libraries. Exposed as hand-created static `PersistentVolume`s per app (`immich-photos-pv`, `media-library-pv`, `paperless-docs-pv`, `audiobookshelf-library-pv`), bound via matching PVCs with `storageClassName: ""`.

No node-local or distributed-replica storage (Longhorn, Rook/Ceph, local-path-provisioner) is used for anything that needs to survive a pod reschedule.

## Alternatives Considered

**Longhorn** (distributed block storage, replicated across node-local disks)
Rejected. Nodes have one thin-provisioned `local-lvm` disk each with no capacity set aside for replica storage — running Longhorn here would mean 2–3x storage overhead (default replica count) competing for the same disk/CPU/network budget the actual workloads use, on VMs that are already tight on memory. Replicating multi-TB volumes (Immich's 2Ti, media's 1Ti) three times across three modest VMs is expensive for no real benefit when a NAS with its own redundancy already exists. Longhorn's per-node manager/engine pods and their control plane are also nontrivial overhead for a 3-node homelab.

**Rook/Ceph**
Rejected for the same reasons as Longhorn, more so — Ceph generally wants multiple dedicated OSD disks per node and meaningful CPU/RAM headroom to be worth running. Substantially overkill at this scale.

**k3s built-in `local-path-provisioner`**
Rejected. No replication and no `ReadWriteMany` — volumes are pinned to whichever node created them. That's incompatible with a cluster that gets power-cycled and rescheduled regularly, and doesn't support the several apps that need concurrent multi-pod access to the same media library.

**iSCSI from the NAS** (block device per volume instead of NFS)
Considered for the CloudNativePG workloads specifically, since block storage avoids NFS's weaker fsync/locking semantics for a database. Not pursued cluster-wide: iSCSI LUNs are effectively single-mounter (no native `ReadWriteMany`), so it would only replace the small-PVC tier, not the bulk-media tier that actually needs shared access — and it would mean managing per-volume LUNs on the NAS instead of plain NFS exports, more operational overhead for a benefit that doesn't apply to most of the workloads here. May be revisited for CloudNativePG specifically (see Consequences).

**Cloud/object storage (S3-compatible, e.g. MinIO, or a cloud provider)**
Rejected as primary storage. Multi-TB media libraries would mean either paying for cloud capacity/egress or self-hosting another storage layer for no benefit over the NAS that's already there, and apps like Sonarr/Radarr/Jellyfin expect a POSIX filesystem (hardlinks, direct file access), not an object API. Worth reconsidering purely as an off-site _backup_ target later — orthogonal to this decision.

## Why NAS + NFS won

- Several apps require genuine multi-pod concurrent access to the same files (`*arr` stack + qBittorrent + Jellyfin all sharing one media tree; Immich + Immich Power Tools sharing the photo library). NFS's native `ReadWriteMany` is the only option evaluated that supports this without extra machinery.
- The cluster's pods are not node-pinned by design (scheduled wake/shutdown cycles), so storage that isn't tied to a specific node's local disk avoids replica-rebuild/data-locality concerns entirely — a volume is just as reachable regardless of which node a pod lands on.
- The NAS already exists and serves other roles for this homelab (Gitea git hosting, a Wake-on-LAN target in the cluster's startup sequence). Reusing it avoids standing up an entire additional distributed-storage subsystem on nodes that don't have spare resources for one.
- The two-pool split (SSD for hot/small data, bulk pool for cold/large data) is trivial with plain NFS exports and would be considerably more manual to replicate under Longhorn's uniform-node-disk model.
- Centralizes backup surface: one NAS (with ZFS snapshots on its pools) to protect, instead of replicated volumes scattered across three VMs.

## Consequences

- **The NAS is a single point of failure for the entire stateful workload set.** If NAS or the network path to it goes down, effectively every app with persistent state stops working — there is no per-node fallback the way there would be with Longhorn's node-replicated volumes. This is an accepted risk at homelab scale (no budget/need for a redundant NAS), but it is the main tradeoff of this decision and the first thing to revisit if the NAS's reliability becomes a problem.
- **PostgreSQL-on-NFS is a known-risky pattern.** All CloudNativePG clusters (`immich-db` and others) store WAL/data files on the `nfs-db` StorageClass. NFS has weaker fsync/locking guarantees than local block storage, which is generally discouraged for RDBMSes. This is mitigated today by running single-instance Postgres everywhere (`instances: 1`, no HA replicas relying on shared storage semantics) and by the `hard`/`nfsvers=4.1` mount tuning, but it's a conscious tradeoff, not a best practice — if any CloudNativePG cluster moves to multi-instance HA, its storage should be reconsidered (e.g. iSCSI LUNs or node-local block storage) rather than assumed to work the same way on NFS.
- **Every stateful read/write crosses the network** instead of hitting local disk. `nconnect=8` and a wired LAN keep this reasonable, but it's inherently higher latency than local NVMe.
- **Cluster startup ordering now has a hard dependency on the NAS being reachable first.** This is already accounted for — `homelab-cluster-agent`'s `wake-hosts.sh` phase waits for the NAS to come up (checked via TCP to its HTTPS port) before the Kubernetes-level readiness phases run — but it's a direct structural consequence of this decision worth remembering if that startup sequence is ever changed.
- **No Kubernetes-native volume snapshotting.** `csi-driver-nfs` doesn't provide CSI snapshot support here; backup/recovery depends entirely on whatever snapshot capability the NAS's underlying ZFS pools provide externally, not on `VolumeSnapshot` objects in-cluster.

## Revisit if

- The NAS's reliability or capacity becomes a recurring problem (would push toward a redundant NAS, or reintroducing Longhorn/Ceph for a subset of workloads).
- Any CloudNativePG cluster needs multi-instance HA — its storage should be re-evaluated separately from this decision rather than staying on `nfs-db` by default.
- Node count/local disk capacity grows enough that distributed replicated storage stops being disproportionately expensive relative to the cluster's size.
