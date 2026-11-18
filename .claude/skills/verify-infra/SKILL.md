---
name: verify-infra
description: Run static validation checks (kustomize registration, ansible syntax, Traefik env-var wiring) against currently changed files in this repo. Use before staging changes, or whenever asked to double-check infra changes are wired up correctly.
---

Runs `~/.claude/scripts/homelab-verify.sh` against the current working tree's changes (same script the `PreToolUse` hook on `git add` runs automatically). Useful to re-check mid-work, or after fixing a failure, without needing to attempt `git add` again.

This does NOT replace reading the diff for logic/correctness — it only catches a specific, narrow class of silent wiring failures: a new `apps/*.yaml` file never added to `apps/kustomization.yaml`'s `resources:` list, a Traefik dynamic-config `{{ env "VAR" }}` reference never added to `compose.yaml`'s `environment:` block, Ansible syntax errors, and (in `homelab-provisioning`) Terraform schema errors via `terraform validate`.

Run it:
!`echo '{"tool_input":{"command":"git add --dry-run ."}}' | ~/.claude/scripts/homelab-verify.sh 2>&1 || true`
