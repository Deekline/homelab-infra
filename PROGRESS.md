# Migration Progress Log

This file documents every step taken during the k3s migration, with the reason behind each decision.
Update this file as each step is completed.

---

## Completed Steps

### Phase 0 — Bootstrap on Arch PC (`10.0.50.119`, VLAN50)

---

#### ✅ k3s installed on Arch PC

**Why:** Arch PC is the free bootstrap node — we build and test the entire stack here before buying M910Q hardware. Mistakes here cost nothing.

**Gotcha:** k3s is not officially supported on Arch. Works fine, but watch for systemd or kernel module edge cases when debugging.

**Gotcha:** k3s ships with its own Traefik. Must be disabled before installing ours or you get IngressClass conflicts:
```bash
sudo touch /var/lib/rancher/k3s/server/manifests/traefik.yaml.skip
kubectl delete helmchart traefik traefik-crd -n kube-system
kubectl delete ingressclass traefik
```

---

#### ✅ SOPS + age configured

**Why:** All k8s Secrets are encrypted at rest in Git. Only `data`/`stringData` fields are encrypted — metadata stays readable so ArgoCD can parse the files.

**Setup:**
```bash
# Install
pacman -S sops age

# Generate keypair (one-time)
age-keygen -o ~/.config/sops/age/keys.txt

# .sops.yaml at infra/ root tells sops which key to use for *secrets.yaml files
```

**Key locations:**
- Private key: `~/.config/sops/age/keys.txt` — never commit, back up offline
- Public key: in `infra/.sops.yaml` (safe to commit)

**Usage:**
```bash
sops infra/k3s/<service>/secrets.yaml          # edit in-place
sops -d infra/k3s/<service>/secrets.yaml        # decrypt to stdout
sops -d infra/k3s/<service>/secrets.yaml | sudo kubectl apply -f -
```

---

#### ✅ cert-manager installed

**Why:** Manages the wildcard TLS certificate (`*.d-kline.org`) via Cloudflare DNS-01 challenge. Handles auto-renewal. Traefik reads the resulting k8s Secret directly.

**Install:**
```bash
helm repo add jetstack https://charts.jetstack.io && helm repo update
helm install cert-manager jetstack/cert-manager -n cert-manager \
  --create-namespace \
  -f infra/k3s/cert-manager/values.yaml
```

**Key config (`values.yaml`):**
- `--dns01-recursive-nameservers=1.1.1.1:53,8.8.8.8:53` — uses public resolvers instead of walking the authoritative NS chain (faster, more reliable)
- `crds.enabled: true` — installs CRDs as part of Helm release

**Apply secrets + ClusterIssuer:**
```bash
# Cloudflare credentials must exist in BOTH namespaces:
# - traefik: where the Certificate resource lives
# - cert-manager: where the ClusterIssuer looks for them
sops -d infra/k3s/cert-manager/secrets.yaml | sudo kubectl apply -f -
sops -d infra/k3s/cert-manager/secrets.yaml | sed 's/namespace: traefik/namespace: cert-manager/' | sudo kubectl apply -f -

# Create ClusterIssuer + Certificate
sudo kubectl apply -f infra/k3s/cert-manager/cert-manager.yaml

# Monitor cert issuance (takes 2-5 min for DNS propagation)
sudo kubectl get certificate -n traefik -w
```

---

#### ✅ Traefik installed

**Why:** Reverse proxy and SSL termination for all services. Docker label-based routing replaced by IngressRoute CRDs. Single entry point for all traffic.

**Install:**
```bash
helm repo add traefik https://traefik.github.io/charts && helm repo update
kubectl create namespace traefik

# Apply dashboard basic auth secret first
sops -d infra/k3s/traefik/secrets.yaml | sudo kubectl apply -f -

helm install traefik traefik/traefik -n traefik \
  -f infra/k3s/traefik/values.yaml

# Apply CRDs
sudo kubectl apply -f infra/k3s/traefik/middlewares.yaml
sudo kubectl apply -f infra/k3s/traefik/dashboard.yaml
sudo kubectl apply -f infra/k3s/traefik/external-services.yaml
```

**What's deployed:**
- Middlewares: `local-only` (IP allowlist), `security-headers`, `rate-limit`, `traefik-auth` (basic auth)
- Dashboard at `traefik.d-kline.org` — local-only + basic auth
- External routes: Proxmox (`10.0.10.2:8006`), TrueNAS (`10.0.10.150:443`), OPNsense (`10.0.10.1:80`)
- CrowdSec plugin loaded but **inactive** — activate after CrowdSec is deployed

**Verify:**
```bash
sudo kubectl get pods -n traefik
sudo kubectl get certificate -n traefik       # READY=True
sudo kubectl get ingressroute -n traefik
sudo kubectl get middleware -n traefik
```

---

#### ✅ Gitea installed on TrueNAS

**Why:** Git server must be independent of k3s — it's what ArgoCD pulls from. If Gitea ran on k3s, a cluster failure would make recovery impossible (can't pull the fix from Git if Git is down).

**Location:** TrueNAS app, accessible at `http://10.0.10.150:30008`

**Repos created:**
- `homelab-infra` — k3s manifests, ArgoCD apps, Helm values (this repo)
- `homelab-ansible` — node provisioning playbooks
- `homelab-terraform` — Proxmox VM definitions

---

#### ✅ ArgoCD installed

**Why:** GitOps controller. Watches Gitea and reconciles cluster state automatically. After ArgoCD is running, no manual `kubectl apply` or `helm upgrade` in production — push to Git instead.

**Install:**
```bash
helm repo add argo https://argoproj.github.io/argo-helm && helm repo update
helm install argocd argo/argo-cd -n argocd \
  --create-namespace \
  -f infra/k3s/argocd/values.yaml
```

**Key config (`values.yaml`):**
- `server.insecure: true` — TLS terminated by Traefik, not ArgoCD itself
- `server.ingress.enabled: false` — using Traefik IngressRoute instead
- SOPS age key mounted at `/sops/age/keys.txt` in repo-server — allows ArgoCD to decrypt SOPS secrets during sync

**Apply age key secret (one-time):**
```bash
sudo kubectl create secret generic sops-age-key \
  --from-file=keys.txt=$HOME/.config/sops/age/keys.txt \
  -n argocd
```

**Gitea repo credentials — SSH only, applied manually (see SOPS secrets section below):**
```bash
sudo kubectl create secret generic gitea-homelab-infra \
  --from-literal=type=git \
  --from-literal=url=ssh://git@10.0.10.150:30009/deekline/homelab-infra.git \
  --from-file=sshPrivateKey=$HOME/.ssh/argocd_gitea \
  -n argocd
sudo kubectl label secret gitea-homelab-infra -n argocd \
  argocd.argoproj.io/secret-type=repository
```

**IngressRoute** at `argocd.d-kline.org` — local-only, TLS via `d-kline-org-tls`.

---

#### ✅ ArgoCD Application manifests created

**Why:** Instead of clicking through the ArgoCD UI to register each app, Application CRDs are stored in Git (`apps/`). ArgoCD watches this directory and manages itself from Git — no manual UI configuration. This is the "App of Apps" pattern.

**Pattern:** One `apps/<service>.yaml` per service. Each file tells ArgoCD:
- Where the manifests are (Gitea repo + path)
- Which namespace to deploy into
- To ignore Secret drift (secrets are applied manually via SOPS, not managed by ArgoCD)

**Apply once (then ArgoCD self-manages):**
```bash
sudo kubectl apply -f infra/apps/traefik.yaml
sudo kubectl apply -f infra/apps/cert-manager.yaml
```

**After applying:** apps appear in ArgoCD UI automatically. Any future `git push` to `k3s/traefik/` or `k3s/cert-manager/` triggers an automatic sync.

**Upgrade ArgoCD after values change:**
```bash
helm upgrade argocd argo/argo-cd -n argocd -f infra/k3s/argocd/values.yaml
```

---

#### ✅ Gitea repo credentials for ArgoCD (private repo)

**Why:** `homelab-infra` is a private Gitea repo. ArgoCD needs credentials to pull from it. Stored as a SOPS-encrypted k8s Secret in Git rather than configured manually in the UI — keeps everything in Git.

**The `argocd.argoproj.io/secret-type: repository` label** is what tells ArgoCD this Secret is a repo credential. No UI config needed.

**Steps:**
1. Generate a Gitea access token: Gitea → Settings → Applications → Generate Token (read repo scope)
2. Create and encrypt the secret:
```bash
cat <<EOF > infra/k3s/argocd/repo-secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: gitea-homelab-infra
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: http://10.0.10.150:30008/deekline/homelab-infra
  username: deekline
  password: <gitea-token>
EOF

sops --encrypt --in-place infra/k3s/argocd/repo-secret.yaml
```
3. Apply:
```bash
sops -d infra/k3s/argocd/repo-secret.yaml | sudo kubectl apply -f -
```

---

#### ✅ ArgoCD self-management Application manifest

**Why:** ArgoCD manages its own config (Helm values, repo secret, IngressRoute) from Git via `apps/argocd.yaml`. Any change to `k3s/argocd/` is auto-synced — no manual `helm upgrade` needed.

**Apply once:**
```bash
sudo kubectl apply -f infra/apps/argocd.yaml
```

---

#### ✅ ArgoCD connected to Gitea via SSH

**Why:** HTTP basic auth with Gitea tokens proved unreliable with ArgoCD — repeated Unauthorized errors even with valid tokens. SSH key auth is more reliable and doesn't have token expiry issues.

**Gotchas encountered during HTTP setup (don't repeat these):**
- Gitea HTTP port is `30008`, SSH port is `30009` — not the default `3000`
- ArgoCD requires `.git` suffix on the repo URL
- Gitea token needs `read:repository` scope — without it returns `403 Forbidden` not `401`
- ArgoCD repo secret `stringData` must be a top-level field, not nested under `metadata`
- Never directly edit a SOPS-encrypted file with a text editor — it breaks the MAC. Always use `sops <file>` to edit. If MAC is broken: `sops -d --ignore-mac <file>` to recover
- ArgoCD overwrites repo secrets during sync with encrypted ciphertext — added `argocd.argoproj.io/sync-options: Skip=true` annotation to prevent this
- ArgoCD application-controller is a **StatefulSet**, not a Deployment: `kubectl rollout restart statefulset argocd-application-controller -n argocd`

**SSH setup steps:**
```bash
# 1. Generate dedicated SSH key for ArgoCD
ssh-keygen -t ed25519 -C "argocd" -f ~/.ssh/argocd_gitea -N ""

# 2. Add public key to Gitea repo as deploy key
# Gitea → homelab-infra repo → Settings → Deploy Keys → Add Key (read-only)

# 3. Delete old HTTP secret, create SSH secret
sudo kubectl delete secret gitea-homelab-infra -n argocd
sudo kubectl create secret generic gitea-homelab-infra \
  --from-literal=type=git \
  --from-literal=url=ssh://git@10.0.10.150:30009/deekline/homelab-infra.git \
  --from-literal=sshPrivateKey="$(cat ~/.ssh/argocd_gitea)" \
  -n argocd
sudo kubectl label secret gitea-homelab-infra -n argocd \
  argocd.argoproj.io/secret-type=repository

# 4. Get Gitea SSH host key
ssh-keyscan -p 30009 10.0.10.150
```

**Add host key to ArgoCD values** (`k3s/argocd/values.yaml`):
```yaml
configs:
  ssh:
    extraHosts: |
      [10.0.10.150]:30009 ssh-rsa <key>
```

**Apply via Helm** (one-time, until ArgoCD can sync itself):
```bash
helm upgrade argocd argo/argo-cd -n argocd -f k3s/argocd/values.yaml
```

**All app manifests use SSH URL:**
```
ssh://git@10.0.10.150:30009/deekline/homelab-infra.git
```

---

#### ✅ sops-secrets-operator installed (cluster-side SOPS decryption)

**Why:** ArgoCD has no SOPS decryption plugin. If ArgoCD applies a SOPS-encrypted `kind: Secret`, the cluster gets ciphertext as actual secret values — garbage passwords. Two approaches exist:
- **ArgoCD plugin (not used):** decrypt during manifest generation — runs on ArgoCD server, complex setup
- **sops-secrets-operator (used):** decrypt on the cluster — ArgoCD applies `kind: SopsSecret` CRDs (encrypted, safe), the operator decrypts them into real `kind: Secret`

This is the approach recommended by ArgoCD's own documentation ("strongly recommend the former").

**How it works:**
1. Secrets in Git are `kind: SopsSecret` (not `kind: Secret`) — ArgoCD applies them as-is
2. Operator watches for `SopsSecret` CRDs and decrypts them using the age key mounted from `sops-age-key` secret
3. Operator creates the corresponding `kind: Secret` with real values

**Bootstrap order (one-time):**
```bash
# 1. Create namespace and age key secret BEFORE helm install
#    (operator pod needs the secret to exist on startup)
sudo kubectl create namespace sops-secrets-operator
sudo kubectl create secret generic sops-age-key \
  --from-file=keys.txt=$HOME/.config/sops/age/keys.txt \
  -n sops-secrets-operator

# 2. Install the operator
helm repo add sops-secrets-operator https://isindir.github.io/sops-secrets-operator/
helm repo update
helm install sops-operator sops-secrets-operator/sops-secrets-operator \
  -n sops-secrets-operator \
  -f infra/k3s/sops-operator/values.yaml

# 3. Apply ArgoCD Application to manage it going forward
sudo kubectl apply -f infra/apps/sops-operator.yaml
```

**Operator values** (`k3s/sops-operator/values.yaml`): mounts `sops-age-key` secret at `/etc/sops-age/keys.txt` and sets `SOPS_AGE_KEY_FILE` env var.

**ArgoCD Application** (`apps/sops-operator.yaml`): uses multi-source — Helm chart from `isindir.github.io/sops-secrets-operator/` with values from Git. Requires ArgoCD 2.6+.

**Why never edit SOPS files directly:** SOPS computes a MAC over the entire file. Editing with a text editor breaks the MAC — next `sops -d` fails with "MAC mismatch". Recovery: `sops -d --ignore-mac <file>`. Always use `sops <file>` to edit.

**Bootstrap secrets — never in Git at all:**

Two secrets are applied manually and never stored in any ArgoCD-managed path (circular dependencies):
```bash
# 1. age key for operator — in sops-secrets-operator namespace (see above)

# 2. Gitea SSH credentials — in argocd namespace
sudo kubectl create secret generic gitea-homelab-infra \
  --from-literal=type=git \
  --from-literal=url=ssh://git@10.0.10.150:30009/deekline/homelab-infra.git \
  --from-file=sshPrivateKey=$HOME/.ssh/argocd_gitea \
  -n argocd
sudo kubectl label secret gitea-homelab-infra -n argocd \
  argocd.argoproj.io/secret-type=repository
```

**All other secrets** use `kind: SopsSecret` in Git — ArgoCD applies the CRD, operator decrypts it. No manual `kubectl apply` needed after initial encryption + commit.

**Encrypting secrets:**
```bash
# Always encrypt before committing — plaintext in Git = exposed credentials
sops --encrypt --in-place k3s/traefik/secrets.yaml
sops --encrypt --in-place k3s/cert-manager/secrets.yaml

# To edit an existing encrypted file:
sops k3s/<service>/secrets.yaml
```

---

#### ✅ Certificate issuer mismatch fixed

**Why:** Two bugs caused the wildcard cert (`d-kline-org-wildcard`) to show as degraded in ArgoCD.

**Bug 1:** `traefik/certificate.yaml` referenced issuer `letsencrypt-prod` but the ClusterIssuer is named `cloudflare-letsencrypt`. Fixed by updating the issuerRef name.

**Bug 2:** There were two Certificate resources targeting the same secret `d-kline-org-tls` — one in `cert-manager.yaml` and one in `traefik/certificate.yaml`. Removed the duplicate from `cert-manager.yaml`. The Certificate belongs in the traefik path since it lives in the `traefik` namespace.

**Fix:**
```bash
# Delete the orphaned CertificateRequest created with wrong issuer
sudo kubectl delete certificaterequest d-kline-org-wildcard-1 -n traefik

# cert-manager auto-creates a new one with correct issuer after the Certificate is fixed
sudo kubectl get certificate -n traefik -w   # wait for READY=True
```

**Cloudflare credentials must exist in both namespaces:**
```bash
sops -d k3s/cert-manager/secrets.yaml | sudo kubectl apply -f -
sops -d k3s/cert-manager/secrets.yaml | sed 's/namespace: traefik/namespace: cert-manager/' | sudo kubectl apply -f -
```

---

#### ✅ ArgoCD automated sync enabled

**Why:** Without `syncPolicy.automated`, ArgoCD only detects drift but never acts on it — every git push still requires a manual sync click in the UI. With automated sync, pushing to Git is the only action needed.

**Config on all `apps/*.yaml`:**
```yaml
syncPolicy:
  automated:
    prune: true      # delete resources removed from Git
    selfHeal: true   # revert manual cluster changes back to Git state
  syncOptions:
    - CreateNamespace=true
```

**Special case — `apps/traefik.yaml`** has `ignoreDifferences` for `Endpoints/.subsets` because Traefik's LoadBalancer service generates Endpoint entries that would otherwise cause constant drift.

---

#### ✅ ArgoCD fully operational

**Status:** ArgoCD is connected to Gitea via SSH, syncing all apps automatically. Certificate `d-kline-org-wildcard` is `READY=True`. Cloudflare credentials secret exists in both namespaces. All infrastructure apps syncing green.

**Verified working:**
- `argocd.d-kline.org` — ArgoCD UI, local-only, TLS via wildcard cert
- `traefik.d-kline.org` — dashboard, local-only + basic auth
- `proxmox.d-kline.org`, `nas.d-kline.org`, `opnsense.d-kline.org` — external service routes
- Automated sync: push to Git → cluster reconciles within ~3 minutes

---

## Pending Steps

#### ✅ sops-secrets-operator installed

Secrets files encrypted, operator running, ArgoCD managing operator via `apps/sops-operator.yaml`.

### 🔴 Next: Gitea Actions runner + CI pipeline

**Components created:**
- `k3s/gitea-runner/` — Deployment (act_runner + DinD sidecar), ConfigMap, SopsSecret
- `apps/gitea-runner.yaml` — ArgoCD Application
- `.gitea/workflows/validate.yaml` — CI pipeline (3 jobs: SOPS check, kubeconform, Trivy)
- `infra/.trivyignore` — accepted findings (DinD privileged container)

**Bootstrap steps:**

```bash
# 1. Get registration token from Gitea
# Gitea → Site Administration → Runners → Create runner
# Copy the token

# 2. Fill in token and encrypt
sops infra/k3s/gitea-runner/secrets.yaml
# Replace YOUR_GITEA_RUNNER_REGISTRATION_TOKEN with real token, save+quit

sops --encrypt --in-place infra/k3s/gitea-runner/secrets.yaml

# 3. Commit and push
git add -A
git commit -m "Add Gitea Actions runner and CI validation pipeline"
git push

# 4. Apply ArgoCD Application
sudo kubectl apply -f infra/apps/gitea-runner.yaml

# 5. Verify runner appears in Gitea
# Gitea → Site Administration → Runners — should show k3s-runner as Online
```

**What CI validates on every push:**
1. **SOPS check** — fails if any `*secrets.yaml` has plaintext `stringData:` without SOPS ciphertext
2. **kubeconform** — validates all k8s manifests against API schemas; CRDs looked up from datreeio catalog
3. **Trivy config scan** — fails on HIGH/CRITICAL misconfiguration findings in manifests

**Why DinD:** act_runner needs Docker to run `container:` jobs (each workflow job runs in its own container image). k3s uses containerd, not Docker — DinD provides the Docker daemon inside the pod. The privileged requirement is documented in `.trivyignore`.

### Next: CloudNativePG operator
Postgres operator — one isolated Postgres cluster per app, data on TrueNAS `tank_fast` NFS.

### Then: Dragonfly
Redis-compatible cache. Shared across all apps (Authentik, Immich, Paperless, Nextcloud).

### Then: Authentik
SSO — deploy before any user-facing services. Retrofitting auth later is painful.

### Then: Zot + Gitea Actions runner + Trivy
Private container registry + CI pipeline. After this, the full DevSecOps pipeline is active.

### Then: Service migration
One service at a time. DNS cutover via OPNsense Unbound (instant rollback).

### Future: Monitoring VM
Prometheus + Grafana + Loki on dedicated Proxmox HA VM (`10.0.10.20`). Provisioned after M910Q cluster exists.
