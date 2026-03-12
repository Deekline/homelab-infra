# Traefik on k3s - Setup Documentation

Migrated from Docker Compose (`traefik/`) to Helm chart on k3s.

## Architecture

```
Internet/LAN --> k3s LoadBalancer (80/443)
                        |
                    Traefik Pod
                        |
            +-----------+-----------+
            |           |           |
      IngressRoutes  Middlewares  External Services
      (k8s CRDs)    (k8s CRDs)  (headless svc + endpoints)
            |
      cert-manager --> Cloudflare DNS --> Let's Encrypt
                       (wildcard cert: *.d-kline.org)
```

## Directory Structure

```
infra/
├── .sops.yaml                          # SOPS encryption rules for all secrets
└── k3s/
    ├── cert-manager/
    │   ├── values.yaml                 # Helm values (DNS resolver config)
    │   ├── cert-manager.yaml           # ClusterIssuer + Certificate CRDs
    │   └── secrets.yaml                # Cloudflare API credentials (SOPS-encrypted)
    └── traefik/
        ├── values.yaml                 # Helm values for traefik/traefik chart
        ├── middlewares.yaml            # Middleware CRDs (local-only, security-headers, etc.)
        ├── dashboard.yaml              # IngressRoute for Traefik dashboard
        ├── external-services.yaml      # Routes to non-k8s hosts (Proxmox, TrueNAS, etc.)
        └── secrets.yaml                # Dashboard basic auth (SOPS-encrypted)
```

## Prerequisites

- k3s installed with `--disable traefik` (prevents conflict with built-in Traefik)
- If k3s already installed, disable built-in Traefik:
  ```bash
  sudo touch /var/lib/rancher/k3s/server/manifests/traefik.yaml.skip
  kubectl delete helmchart traefik traefik-crd -n kube-system
  kubectl delete ingressclass traefik
  ```
- Helm repos added:
  ```bash
  helm repo add traefik https://traefik.github.io/charts
  helm repo add jetstack https://charts.jetstack.io
  helm repo update
  ```
- SOPS + age installed: `pacman -S sops age`
- age keypair at `~/.config/sops/age/keys.txt`

## Deployment Order

### 1. Create namespace

```bash
kubectl create namespace traefik
```

### 2. Install cert-manager

```bash
helm install cert-manager jetstack/cert-manager -n cert-manager \
  --create-namespace \
  -f infra/k3s/cert-manager/values.yaml
```

The values file configures DNS resolvers (`1.1.1.1`, `8.8.8.8`) for ACME DNS-01 propagation checks. Without this, cert-manager walks the authoritative NS chain which can be slow or fail.

### 3. Deploy secrets

Cloudflare credentials must exist in **both** `traefik` and `cert-manager` namespaces:
- `traefik` namespace: where the Certificate resource lives and challenges are solved
- `cert-manager` namespace: where the ClusterIssuer looks for secrets

```bash
# Cloudflare credentials -> traefik namespace (for challenge solving)
sops -d infra/k3s/cert-manager/secrets.yaml | kubectl apply -f -

# Cloudflare credentials -> cert-manager namespace (for ClusterIssuer)
sops -d infra/k3s/cert-manager/secrets.yaml | sed 's/namespace: traefik/namespace: cert-manager/' | kubectl apply -f -

# Dashboard basic auth -> traefik namespace
sops -d infra/k3s/traefik/secrets.yaml | kubectl apply -f -
```

### 4. Create ClusterIssuer and Certificate

```bash
kubectl apply -f infra/k3s/cert-manager/cert-manager.yaml
```

This creates:
- `ClusterIssuer/cloudflare-letsencrypt` - ACME DNS-01 via Cloudflare
- `Certificate/d-kline-org-tls` - wildcard cert for `d-kline.org` and `*.d-kline.org`

Verify certificate is issued:
```bash
kubectl get certificate -n traefik -w
# Wait for READY=True (typically 2-5 minutes for DNS propagation)
```

### 5. Install Traefik

```bash
helm install traefik traefik/traefik -n traefik \
  -f infra/k3s/traefik/values.yaml
```

### 6. Apply CRDs

```bash
kubectl apply -f infra/k3s/traefik/middlewares.yaml
kubectl apply -f infra/k3s/traefik/dashboard.yaml
kubectl apply -f infra/k3s/traefik/external-services.yaml
```

## Verification

```bash
# Traefik pod running
kubectl get pods -n traefik

# TLS cert ready
kubectl get certificate -n traefik

# IngressRoutes registered
kubectl get ingressroute -n traefik

# Middlewares loaded
kubectl get middleware -n traefik

# Dashboard accessible
curl -k https://traefik.d-kline.org
```

## Components

### Traefik Helm Values (`values.yaml`)

| Setting | Value | Notes |
|---------|-------|-------|
| Image | `traefik:v3.6` | Pinned version |
| Entrypoints | web (80), websecure (443) | HTTP auto-redirects to HTTPS |
| TLS | cert-manager (not Traefik ACME) | Wildcard cert via `d-kline-org-tls` secret |
| Service type | LoadBalancer | Uses k3s ServiceLB/klipper |
| Providers | kubernetesCRD, kubernetesIngress | Replaces Docker provider |
| Metrics | Prometheus | Entrypoint, router, and service labels |
| Access logs | JSON format | Same as Docker setup |
| Plugins | CrowdSec bouncer (loaded, inactive) | Enable when CrowdSec is deployed |
| Security | no-new-privileges, read-only rootfs | Drop all capabilities |

### Middlewares (`middlewares.yaml`)

| Middleware | Type | Purpose |
|------------|------|---------|
| `local-only` | ipAllowList | Restrict to `10.0.0.0/8`, `172.0.0.0/8` |
| `security-headers` | headers | HSTS, XSS filter, frame deny, content-type nosniff |
| `rate-limit` | rateLimit | 100 avg, 200 burst |
| `traefik-auth` | basicAuth | Dashboard authentication (reads `traefik-dashboard-auth` secret) |

### External Services (`external-services.yaml`)

Routes to hosts outside the k3s cluster using headless Services + manual Endpoints (not ExternalName, which doesn't work with raw IPs).

| Service | Address | Middlewares | ServersTransport |
|---------|---------|-------------|------------------|
| Proxmox | `10.0.10.2:8006` | local-only | ignorecert (skip TLS verify) |
| TrueNAS | `10.0.10.150:443` | local-only | nasignore (skip TLS verify) |
| OPNsense | `10.0.10.1:80` | local-only | - |

## Secrets Management (SOPS + age)

All secrets are encrypted with SOPS using age encryption. The `.sops.yaml` at `infra/.sops.yaml` defines encryption rules: only `data` and `stringData` fields are encrypted, metadata stays readable.

### Editing secrets

```bash
# Edit in-place (opens in $EDITOR, encrypts on save)
sops infra/k3s/traefik/secrets.yaml
sops infra/k3s/cert-manager/secrets.yaml

# Decrypt to stdout
sops -d infra/k3s/traefik/secrets.yaml
```

### Secret locations

| Secret | Namespace | Contents |
|--------|-----------|----------|
| `cloudflare-credentials` | `traefik` + `cert-manager` | CF email + API token |
| `traefik-dashboard-auth` | `traefik` | htpasswd basic auth |
| `d-kline-org-tls` | `traefik` | Auto-managed by cert-manager (TLS cert + key) |
| `cloudflare-letsencrypt-key` | `cert-manager` | Auto-managed by cert-manager (ACME account key) |

### Key management

- Private key: `~/.config/sops/age/keys.txt` - **never commit this, back up securely**
- Public key: `age1vnjflcl0aeefw82f5klz764c4r5ddg9ad63y7vmu58dxmw026sms047q3j`

## Key Differences from Docker Setup

| Aspect | Docker Compose | k3s/Helm |
|--------|---------------|----------|
| Service discovery | Docker labels | IngressRoute CRDs |
| TLS certificates | Traefik ACME (built-in) | cert-manager + ClusterIssuer |
| Middlewares | File provider (`dynamic/`) | Middleware CRDs |
| External services | File provider (`services.yml`) | Headless Service + Endpoints + IngressRoute |
| Secrets | `.env` files | SOPS-encrypted k8s Secrets |
| Auth | Authentik forward auth | Not yet (add when Authentik is on k3s) |

## Gotchas / Lessons Learned

1. **k3s ships with Traefik** - must disable before installing your own, or you get `IngressClass` conflicts.

2. **Helm chart schema is strict** - redirect is `ports.web.http.redirections.entryPoint`, not `redirectTo`. Plugins go under `experimental.plugins`, not `additionalArguments`.

3. **ExternalName doesn't work with IPs** - Kubernetes ExternalName creates a DNS CNAME, which can't point to an IP. Use headless Service (`clusterIP: None`) + manual Endpoints instead.

4. **cert-manager ClusterIssuer secret resolution** - When solving DNS-01 challenges for a Certificate in namespace X, cert-manager looks for the referenced secret in **both** the Certificate's namespace (for challenges) and the cert-manager namespace (for the ClusterIssuer). The secret must exist in both.

5. **DNS propagation checks** - cert-manager defaults to checking authoritative nameservers, which can be slow. Set `--dns01-recursive-nameservers=1.1.1.1:53,8.8.8.8:53` and `--dns01-recursive-nameservers-only` in cert-manager values to use public resolvers instead.

## Upgrading

```bash
# Traefik
helm upgrade traefik traefik/traefik -n traefik -f infra/k3s/traefik/values.yaml

# cert-manager
helm upgrade cert-manager jetstack/cert-manager -n cert-manager -f infra/k3s/cert-manager/values.yaml

# CRDs (re-apply after changes)
kubectl apply -f infra/k3s/traefik/middlewares.yaml
kubectl apply -f infra/k3s/traefik/dashboard.yaml
kubectl apply -f infra/k3s/traefik/external-services.yaml
kubectl apply -f infra/k3s/cert-manager/cert-manager.yaml
```

## Next Steps

- [ ] Deploy Authentik to k3s, then add `authentik` Middleware back to IngressRoutes
- [ ] Set up ArgoCD for GitOps (auto-sync from Gitea)
- [ ] Enable CrowdSec bouncer once CrowdSec is deployed
- [ ] Migrate Docker Compose services one by one (each gets Deployment + Service + IngressRoute)
