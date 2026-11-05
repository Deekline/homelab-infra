#!/bin/sh
set -eu

REPOS="homelab-infra homelab-provisioning homelab-docs homelab-ai-copilot"
: "${GITEA_HOST:?}"
: "${GITEA_SSH_PORT:?}"
: "${GITEA_USER:?}"

echo "${DEPLOY_KEY_B64}" | base64 -d > "${HOME}/.ssh/id_ed25519"
chmod 600 "${HOME}/.ssh/id_ed25519"
ssh-keyscan -p "${GITEA_SSH_PORT}" "${GITEA_HOST}" > "${HOME}/.ssh/known_hosts" 2>/dev/null

sync_once() {
  for repo in ${REPOS}; do
    dest="/repos/${repo}"
    if [ -d "${dest}/.git" ]; then
      git -C "${dest}" pull --ff-only --quiet || echo "WARN: pull failed for ${repo}"
    else
      git clone --quiet "ssh://git@${GITEA_HOST}:${GITEA_SSH_PORT}/${GITEA_USER}/${repo}.git" "${dest}" \
        || echo "WARN: clone failed for ${repo}"
    fi
  done
}

while true; do
  sync_once
  sleep "${REFRESH_INTERVAL_SECONDS}"
done
