#!/usr/bin/env bash
# Préparation d'un VPS Ubuntu (22.04 / 24.04) pour le watcher : cadrage §11.2 et §11.6.
#   - fuseau Europe/Paris, mises à jour de sécurité automatiques ;
#   - Docker Engine + plugin Compose (dépôt officiel), utilisateur de déploiement dans le groupe docker ;
#   - pare-feu : entrant refusé sauf SSH (règles existantes conservées si ufw est déjà actif : VPS mutualisé) ;
#   - SSH par clé uniquement, root et mot de passe désactivés (sauté si aucune clé n'est installée) ;
#   - dossier data/, droits du .env, rotation de data/cron.log.
# Idempotent. Le cron n'est PAS installé ici : voir docs/deploiement.md.
# Usage, depuis le dépôt cloné, connecté avec l'utilisateur de déploiement :  sudo ./deploy/setup-vps.sh
set -euo pipefail

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'erreur : %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer avec sudo"
DEPLOY_USER="${SUDO_USER:-}"
[[ -n "$DEPLOY_USER" && "$DEPLOY_USER" != root ]] \
    || die "lancer via sudo depuis l'utilisateur de déploiement, pas en root direct"
DEPLOY_GROUP="$(id -gn "$DEPLOY_USER")"
DEPLOY_HOME="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || die "script prévu pour Ubuntu (détecté : ${ID:-inconnu})"

step "Fuseau horaire Europe/Paris"
timedatectl set-timezone Europe/Paris
systemctl restart cron   # le démon cron lit le fuseau au démarrage

step "Mises à jour et paquets de base"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get install -y ca-certificates curl git ufw unattended-upgrades sqlite3 logrotate

step "Mises à jour de sécurité automatiques"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
systemctl enable --now unattended-upgrades

step "Docker Engine + plugin Compose"
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
        "https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    echo "Docker déjà installé : $(docker --version)"
fi
systemctl enable --now docker
usermod -aG docker "$DEPLOY_USER"

step "Pare-feu (ufw)"
ufw allow OpenSSH
if ufw status | grep -q "Status: active"; then
    echo "ufw déjà actif : règles existantes conservées (autres projets du VPS)"
else
    ufw default deny incoming
    ufw default allow outgoing
    ufw --force enable
fi
# Le watcher ne publie aucun port. Attention : un port publié par Docker (ports:) contourne ufw.
ufw status verbose

step "SSH : clé uniquement, root et mot de passe désactivés"
if [[ -s "$DEPLOY_HOME/.ssh/authorized_keys" ]]; then
    # 00- : lu avant 50-cloud-init.conf ; pour sshd, la première valeur rencontrée l'emporte.
    cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'CONF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
CONF
    sshd -t
    systemctl reload ssh 2>/dev/null || systemctl restart ssh
    sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
else
    echo "ATTENTION : $DEPLOY_HOME/.ssh/authorized_keys vide ou absent." >&2
    echo "Durcissement SSH sauté pour ne pas te bloquer dehors : installe ta clé puis relance le script." >&2
fi

step "Dossier data/ et .env"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$REPO_DIR/data"
if [[ -f "$REPO_DIR/.env" ]]; then
    chown "$DEPLOY_USER:$DEPLOY_GROUP" "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
    echo ".env : droits 600"
else
    echo ".env absent : à créer (voir docs/deploiement.md), puis : chmod 600 $REPO_DIR/.env"
fi
chmod +x "$REPO_DIR/deploy/cron-run.sh"

step "Rotation de data/cron.log"
cat > /etc/logrotate.d/ai-agent-market-watcher <<CONF
$REPO_DIR/data/cron.log {
    su $DEPLOY_USER $DEPLOY_GROUP
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
CONF
logrotate --debug /etc/logrotate.d/ai-agent-market-watcher >/dev/null 2>&1 \
    || echo "ATTENTION : configuration logrotate refusée, vérifier /etc/logrotate.d/ai-agent-market-watcher" >&2

DEPLOY_UID="$(id -u "$DEPLOY_USER")"
step "Terminé"
if [[ "$DEPLOY_UID" != 1000 ]]; then
    echo "UID de $DEPLOY_USER = $DEPLOY_UID : ajoute WATCHER_UID=$DEPLOY_UID dans .env avant 'docker compose build'."
fi
echo "Déconnecte-toi puis reconnecte-toi pour que l'appartenance au groupe docker soit prise en compte."
