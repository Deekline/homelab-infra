# 3. Use OpenBao + External Secrets Operator for app secrets, migrated incrementally from SopsSecret

## Status

Accepted (reflects `homelab-provisioning/terraform/modules/openbao-lxc`, `homelab-provisioning/ansible/roles/openbao`, `apps/external-secrets.yaml`, `k3s/external-secrets/`, `k3s/external-secrets-config/`, and `docker/edge/traefik/external-services.yaml`'s `openbao` router). Migration complete: every app in this repo that had a `SopsSecret` now has an `ExternalSecret` instead — see Consequences.

## Context

Every app secret in this repo is a `SopsSecret` CR (`isindir.github.com/v1alpha3`), hand-edited via `sops edit k3s/<app>/secrets.yaml` and reconciled into a real `Secret` by `sops-secrets-operator`. This works, but has two real costs:

- **Per-app `sops edit` friction** — no UI, no reference-by-name, every edit touches an encrypted file directly.
- **No way for an AI assistant to help with secret-related work without either touching encrypted files or being handed plaintext.** The existing rule ("never read/decrypt secrets without asking") was policy-only, with no technical backstop.

The goal was a real secrets manager: a UI, reference-by-name access instead of plaintext handling, and a system an assistant can help wire up without ever needing to see a secret's value.

## Decision

**OpenBao** (the Linux Foundation's fully open-source fork of HashiCorp Vault, taken after HashiCorp re-licensed Vault to BSL in 2023), self-hosted, with these specific architectural choices:

- **Dedicated unprivileged LXC** (`terraform/modules/openbao-lxc`, native `bao` binary + systemd via the `openbao` Ansible role) — not a Docker container on the existing `observability` host. OpenBao holds the keys to every other secret in the homelab; it doesn't share a Docker daemon with Grafana, exporters, or an MCP server.
- **Native `bao` CLI/package**, not a hand-rolled systemd unit — the vendor `.deb` ships its own hardened unit, config paths, and KV/TLS bootstrap; the Ansible role installs it and templates `openbao.hcl` rather than reimplementing what the package already does correctly.
- **Manual unsealing, no auto-unseal** — explicit user call. The unseal key and root token live only in 1Password, never on disk, never read by any automation. Trades convenience for not having the one key that unlocks everything sitting on a host that could be compromised.
- **Reachable two ways**: directly on the LAN (`https://openbao.internal:8200`) for non-interactive clients (ESO), and via the existing edge Traefik + Authelia (its own subdomain on the homelab's domain, `mgmt-only` IP allowlist + 2FA, same pattern as `metrics`/`logs`/`alerts`/`vmalert`) for human/browser UI access with a real trusted cert on that hop — no new ingress machinery, reusing what already fronts every other admin tool.
- **External Secrets Operator** (Helm chart, Flux-managed) syncs OpenBao KV v2 data into real k8s `Secret`s via **Kubernetes auth** (OpenBao validates short-lived k3s ServiceAccount tokens, using a static long-lived reviewer token since OpenBao runs outside k3s and can't use an auto-rotating pod identity) — not a static Vault token stored as a SopsSecret.
- **Incremental app migration**, not a cutover. `sops-secrets-operator` and every unmigrated app's `SopsSecret` stay exactly as-is; `ExternalSecret` is adopted app-by-app.

## Alternatives Considered

**HashiCorp Vault**
Rejected on licensing: Vault moved to BSL 1.1 in 2023, no longer OSI open source. OpenBao is the Linux Foundation fork at the last MPL-licensed commit, API-compatible, with the same Kubernetes-auth/KV-v2 model this decision depends on — functionally the same tool, without the governance risk.

**Infisical** (simpler ops — no unseal step, nicer default UI)
Rejected. Considered seriously for the lower operational overhead, but Vault's API/auth model is the more recognized, transferable pattern, and genuinely available Vault-API-compatible self-hosting was the deciding factor over Infisical's smaller ecosystem and non-standard API shape.

**Sharing the `observability` Docker host** (original plan, reversed mid-build)
Rejected after reconsidering blast radius: bundling the one service holding every other secret onto the same Docker daemon as Grafana/exporters/an MCP server was a weaker isolation story than the alternative (a dedicated LXC) cost to set up.

**Auto-unseal via 1Password CLI at runtime, with a USB-key fallback**
Designed, then explicitly dropped in favor of manual unseal (user's call) — no automation touches the unseal key at all now, not even indirectly through a 1Password service-account token on disk.

**Routing OpenBao through the k3s Gateway (Cilium) like every app, instead of the edge Traefik**
Rejected. The Gateway's VIP is the public-facing path (the same wildcard listener every internet-facing app uses) — putting the secrets manager on it risked real internet exposure. The edge Traefik's `mgmt-only`/`local-only` IP-allowlist middleware (already protecting `metrics`/`logs`/`alerts`/`vmalert`) was the correct existing mechanism, not a new one.

**Flux-native SOPS decryption, replacing `sops-secrets-operator` entirely, as part of this same effort**
Noted as a known, separately-tracked future option (see `homelab-docs/homelab-infra/flux-migration.md`), explicitly not bundled into this decision — OpenBao/ESO and `sops-secrets-operator` coexist; nothing about adopting one requires removing the other.

## Why this combination won

- **Real isolation for the one service that matters most.** A dedicated LXC means a compromise of any Docker-hosted app on `observability` doesn't reach OpenBao, and a compromise of OpenBao doesn't reach anything via a shared Docker daemon.
- **No new ingress surface.** Reusing the edge Traefik + Authelia's existing `mgmt-only` pattern instead of building new exposure machinery meant the only genuinely new infrastructure was OpenBao itself and ESO — not a parallel routing/auth system.
- **Kubernetes auth over a static token** means no long-lived Vault credential sitting in a SopsSecret for ESO — exactly the kind of static-secret-in-git pattern this whole project was meant to move away from.
- **Reference-by-name, not plaintext handling.** `ExternalSecret` resources reference a key path; the actual value never passes through a file an assistant would read, closing the original gap this project set out to solve.
- **Incremental, not a rewrite.** Nothing about adopting OpenBao required touching any already-working `SopsSecret`, so the blast radius of this whole rollout was one new LXC, one new k3s operator, and one pilot app — not a repo-wide secrets migration done under pressure.

## Consequences

- **Manual unseal is a real operational step, not a formality.** Every restart of the OpenBao LXC (reboot, service restart) leaves it sealed until someone runs `bao operator unseal` by hand with the key from 1Password. Documented runbook: `homelab-docs/homelab-infra/openbao-eso-tls-san-2026-11-18.md`.
- **The TLS cert is self-signed and must carry a proper SAN, which the vendor package does not generate by default.** Hit for real during this rollout — `openbao`'s `.deb` postinst auto-generates a cert with zero SAN entries, which silently breaks any strict TLS client (ESO's Vault client) while masking the problem for any client that already skips verification (Traefik's `self-signed` transport, the `bao` CLI with `BAO_SKIP_VERIFY`). Fixed by hand for now (see the incident doc above); **the Ansible role does not yet generate the cert itself**, so a future reinstall of the package would silently regenerate the broken cert — known, explicitly deferred gap, not yet fixed.
- **Every app's secret has been migrated.** `dragonfly`, `homarr`, `garage`, `gitea-runner`, `immich-power-tools`, `affine`, `immich`, `n8n`, `paperless`, `speedtest-tracker`, `qbittorrent`, `actual-budget`, the CNPG backup (barman-cloud/Garage S3) credentials for `immich`/`n8n`/`paperless`, and finally `cert-manager`'s own Cloudflare DNS-01 credentials — the single highest-blast-radius secret in the repo, since it's what every public app's TLS cert renewal depends on. `sops-secrets-operator` now has zero `SopsSecret` consumers left in this repo; it's not been removed as a dependency, just has nothing left to reconcile.
- **Old `SopsSecret` files were kept on disk, not `git rm`'d, until each migration was independently confirmed (`SecretSynced: True`, pod/controller health unaffected) in the live cluster** — the file was only dropped from each `kustomization.yaml`'s `resources:` list first, then deleted in a separate follow-up commit once verified. This was deliberate: dropping a resource from a Kustomization prunes it (and its owned `Secret`, via ownerReference cascade) on the next Flux apply, regardless of whether the new `ExternalSecret` has a value ready yet — hit for real with both `gitea-runner`'s and `immich-db-credentials`' `ExternalSecret`s sitting in `SecretSyncedError` for an extended window because the OpenBao value hadn't been written yet when the old secret was pruned. Sequencing matters more than the Secret-swap mechanics themselves.
- **ESO's RBAC/CRD bootstrap needed splitting into two separate Flux `Kustomization`s** (`k3s/external-secrets/` for the namespace/RBAC/Helm-values ConfigMap, `k3s/external-secrets-config/` for the `ClusterSecretStore`) — Flux validates a `Kustomization`'s entire resource batch before applying any of it, so bundling a CRD-dependent resource (the `ClusterSecretStore`, needing ESO's own CRDs) with resources the HelmRelease itself depends on (the values `ConfigMap`) created a circular dependency that blocked both. Worth remembering for any future app that mixes "things the operator needs to install" with "things that need the operator already installed."
- **OpenBao is now the single most sensitive host in this homelab.** Its own LXC, its own isolation, and the manual-unseal friction are all deliberate consequences of that, not oversights.

## Revisit if

- Manual unsealing becomes enough of a recurring burden to reconsider some form of auto-unseal — would need a new tradeoff discussion, not a quiet reversal of the "no automation touches the unseal key" decision.
- The Ansible role gets the proper-SAN cert generation fix (tracked, not yet done) — once it does, a from-scratch reinstall becomes safe, which isn't true today.
- Now that every app is migrated, formally retiring `sops-secrets-operator` itself (removing the controller, not just its consumers) is worth a separate, deliberate decision — not implied by this ADR just because its job is currently done.
