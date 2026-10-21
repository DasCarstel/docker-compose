#!/usr/bin/env bash
# setup-runner.sh — Self-hosted GitHub-Actions-Runner auf dem Docker-Host (CT 100).
#
# Einmalige Ausführung. Env:
#   HESTIA_GITHUB_TOKEN  Runner-Registration-Token (GitHub: Repo → Settings →
#                        Actions → Runners → New self-hosted runner)
#   GH_RUNNER_DIR        Ziel-Ordner (Default /opt/docker-compose/github-runner)
#   GH_REPO_URL          Repo (Default DasCarstel/opencode-coder-env)
#
# GitHub verhindert sudo-run („Must not run with sudo") — Registration & Dienst
# laufen deshalb als unprivilegierter User `github-runner`;ils nur svc.sh
# (systemd-Eintrag) benötigt root.
set -euo pipefail

GH_RUNNER_DIR="${GH_RUNNER_DIR:-/opt/docker-compose/github-runner}"
GH_REPO_URL="${GH_REPO_URL:-https://github.com/DasCarstel/opencode-coder-env}"
RUNNER_NAME="${RUNNER_NAME:-docker-host-hestia}"
RUNNER_VERSION="${RUNNER_VERSION:-2.331.0}"
RUNNER_USER="${RUNNER_USER:-github-runner}"
RUNNER_LABEL="${RUNNER_LABEL:-docker-host}"

: "${HESTIA_GITHUB_TOKEN:?HESTIA_GITHUB_TOKEN fehlt (Runner-Registration-Token)}"

# ── User anlegen ─────────────────────────────────────────────────────────────
if ! id -u "$RUNNER_USER" >/dev/null 2>&1; then
  useradd -m -u 1200 "$RUNNER_USER"
fi
mkdir -p /opt/github-runners/_work "$GH_RUNNER_DIR"
chown "$RUNNER_USER:$RUNNER_USER" /opt/github-runners "$GH_RUNNER_DIR" /opt/github-runners/_work

# ── Runner-Binaries (als user, damit chown-Besitz erhalten bleibt) ───────────
cd "$GH_RUNNER_DIR"
if [ ! -f config.sh ]; then
  echo "→ Lade actions-runner ${RUNNER_VERSION}..."
  curl -fsSL -o runner.tar.gz \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  su "$RUNNER_USER" -s /bin/bash -c "tar xzf runner.tar.gz && rm -f runner.tar.gz"
  chown -R "$RUNNER_USER:$RUNNER_USER" "$GH_RUNNER_DIR"
fi

# ── Registrierung (ohne sudo!) ────────────────────────────────────────────────
if [ ! -f .runner ]; then
  echo "→ Registriere Runner (Label: ${RUNNER_LABEL})..."
  su "$RUNNER_USER" -s /bin/bash -c \
    "$GH_RUNNER_DIR/config.sh --url '$GH_REPO_URL' --token '$HESTIA_GITHUB_TOKEN' --name '$RUNNER_NAME' --labels '$RUNNER_LABEL' --work /opt/github-runners/_work --unattended --replace"
else
  echo "✓ Runner bereits registriert"
fi
chmod -R u+rwX "$GH_RUNNER_DIR" 2>/dev/null || chown -R "$RUNNER_USER:$RUNNER_USER" "$GH_RUNNER_DIR"

# ── systemd-Service (root für svc.sh; läuft danach als github-runner) ────────
if [ ! -f "$GH_RUNNER_DIR/.service" ]; then
  ./svc.sh install "$RUNNER_USER"
fi
./svc.sh start
echo "✓ Runner läuft: $(systemctl is-active "actions.runner.*" | head -1) (Label ${RUNNER_LABEL})"
