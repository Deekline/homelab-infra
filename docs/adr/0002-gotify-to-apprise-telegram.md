# 2. Replace Gotify with Telegram (native integrations + Apprise as a relay)

## Status

Accepted (reflects `k3s/apprise`, `apps/apprise.yaml`, the `telegram` receiver in `k3s/victoria-metrics-stack/values.yaml`, and the `notify-failure` job in `.gitea/workflows/validate.yaml`). Gotify and its `alertmanager-gotify-bridge` translator, and the `igotify` companion app, have been removed entirely.

## Context

Gotify was the previous self-hosted push notification target for this cluster: a dedicated push server with its own Android/iOS/web client, fed by two producers —

- **Alertmanager**, via `alertmanager-gotify-bridge`, a small translator service that reshaped Alertmanager's fixed webhook JSON schema into Gotify's `/message` API format (Alertmanager's generic `webhook_configs` can't be templated, so this translation step was mandatory).
- **CrowdSec**, via its `http_gotify` notification plugin.
- The Gitea Actions CI pipeline (`notify-failure` job), via a hand-written `curl` step.

Gotify's ingress (`gotify.d-kline.org`) was only reachable through the Traefik middleware — i.e. only from inside the home network or over Tailscale. That meant push notifications were invisible unless the phone was actively connected to the tailnet, and required maintaining a dedicated Gotify client app rather than something already installed and used daily.

## Decision

Standardize on **Telegram** as the actual delivery channel, reached two different ways depending on the producer:

- **Native Telegram support, used directly, no relay**: Alertmanager (`telegram_configs`, built in since Alertmanager v0.26) and Gitea (native `Telegram` webhook type) both talk to the Telegram Bot API themselves. No bridge, no extra infrastructure, no payload-translation problem — the previous `alertmanager-gotify-bridge` was deleted outright rather than repointed.
- **Apprise API (`caronc/apprise-api`), used as a relay**, for producers that have no native Telegram integration or that are hand-written scripts we already control the payload shape of: CrowdSec's notification plugin system, and the Gitea CI `notify-failure` step. Apprise runs in `APPRISE_STATEFUL_MODE=simple`, with the actual Telegram destination(s) registered by hand through its Configuration Manager UI (not stored in this repo) under a config key, referenced by producers as `/notify/<key>`.

## Alternatives Considered

**Keep Gotify**
Rejected. Tailscale-only reachability was the core complaint — notifications are useless if they only show up once a device reconnects to the tailnet. It also already needed a bespoke translator (`alertmanager-gotify-bridge`) for its one structured producer, and would need another for anything else with a fixed webhook schema.

**ntfy** (self-hosted, Gotify-like alternative)
Rejected. Same shape of problem as Gotify — its own app, own protocol, no native Telegram output. It would still need something like Apprise sitting in front of it to reach Telegram, at which point it adds nothing over talking to Telegram directly.

**Route every producer through Apprise indiscriminately**
Initially attempted, then reverted. Alertmanager's `webhook_configs` always sends its own fixed JSON schema, which doesn't satisfy Apprise's `/notify` requirement for a `body` field — the call would 400 silently. Discovering Alertmanager (and separately, Gitea) already had first-class Telegram support made this moot: those two producers don't need a relay at all. Apprise was kept only where it's actually earning its place — CrowdSec (no native Telegram plugin) and the CI step (a script we fully control, so there's no payload-shape mismatch to begin with).

**Hand-roll raw Telegram Bot API calls everywhere, no relay at all**
Rejected for CrowdSec and future producers: every new integration would mean writing and maintaining its own Telegram-formatting logic. Apprise centralizes that, and leaves room to fan out to Discord or other services later by changing one config key's registered URLs instead of touching every producer again.

## Why this combination won

- **Cross-platform and already installed.** Telegram needs no dedicated client to maintain — unlike Gotify, which required its own app on every device that should receive alerts.
- **Reachable without Tailscale.** Telegram delivers over the public internet via its own infrastructure; notifications arrive whether or not the receiving device is connected to the home network or tailnet. This was the main practical complaint about Gotify and the primary driver of this decision.
- **Prefer native integrations over a universal relay.** Where a producer already speaks Telegram natively (Alertmanager, Gitea), using that directly is less infrastructure to run and nothing to break — Apprise is deliberately not used as a one-size-fits-all funnel.
- **Apprise fills the real gap.** CrowdSec and ad-hoc scripts (CI) have no native Telegram output; a small shared relay is better than writing bespoke Telegram formatting per producer, and keeps the door open for additional notification channels later without re-touching every producer's config.

## Consequences

- **Apprise is a dependency for CrowdSec and CI notifications specifically** — not for Alertmanager or Gitea's own event notifications, which bypass it entirely and keep working even if Apprise is down.
- **Apprise's `/notify` endpoint has no authentication**, unlike Gotify's per-app `X-Gotify-Key` token. It currently relies on the `local-only` Traefik IP allowlist as its only access control. Tracked as a follow-up in `docs/optimizations.md` (nginx basic auth, since Authelia's SSO redirect flow doesn't work for non-interactive callers like CI or Alertmanager).
- **The Telegram destination is registered by hand, not in git.** Apprise's stateful config lives only on its own PVC; if that volume is ever lost, the bot token/chat ID must be re-entered manually through the Configuration Manager UI. This was a deliberate tradeoff to avoid storing a live bot token in a way any producer's config would need to reference directly.
- **No independent fallback channel.** Gotify's delivery path was entirely separate from any third-party service; now, if Telegram itself has an outage, there is currently no secondary channel configured. Accepted at homelab scale given Telegram's reliability and reach.
- **CrowdSec's notification plugin was removed along with Gotify** and is not yet repointed at Apprise — it currently has no notification target configured at all. Tracked as an open follow-up, not resolved by this ADR.

## Revisit if

- Telegram itself becomes unreliable, blocked, or otherwise unsuitable as the primary channel.
- Apprise's lack of authentication on `/notify` becomes a real exposure concern rather than a theoretical one (see `docs/optimizations.md`).
- A producer needs a notification target Apprise can't relay to well, or the CrowdSec integration gap needs closing.
