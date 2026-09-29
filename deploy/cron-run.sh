#!/usr/bin/env bash
# Run quotidien lancé par le cron de l'hôte (cadrage §11.2) :
#   0 7 * * * /opt/watcher/deploy/cron-run.sh
# Sans argument : commande par défaut du service (`python -m watcher.run --all`). Avec arguments : commande
# passée au conteneur (ex. `deploy/cron-run.sh python -m watcher.run --heartbeat`).
# -T : pas de pseudo-terminal (le cron n'en a pas). Sortie ajoutée à data/cron.log (rotation : logrotate).
#
# Filet de sécurité (run par défaut uniquement) : si la commande se termine en erreur, le script pingue
# lui-même `/fail` sur le check Healthchecks de l'environnement, avec la fin de la sortie en corps. Couvre les
# pannes survenues avant que Python ne pingue (Docker absent, image introuvable, .env illisible...). Si Python
# a déjà pingué `/fail`, ce second ping remplace seulement le corps par la même sortie, qui contient le résumé.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
mkdir -p data
LOG=data/cron.log
PING_BODY_MAX_BYTES=10000

# Valeur d'une clé du .env (dernière occurrence), sans commentaire en fin de ligne ni guillemets.
env_get() {
    [[ -r .env ]] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" .env | tail -n 1 \
        | sed -e 's/[[:space:]]#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# Même choix que Settings.healthchecks_ping_url : en test, uniquement le check dédié.
ping_url() {
    local env
    env="$(env_get WATCHER_ENV)"
    if [[ "${env:-prod}" == test ]]; then env_get HEALTHCHECKS_TEST_URL; else env_get HEALTHCHECKS_URL; fi
}

offset=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
status=0
{
    printf '=== %s ===\n' "$(date -Iseconds)"
    docker compose run --rm -T watcher "$@"
} >> "$LOG" 2>&1 || status=$?

if (( status != 0 && $# == 0 )); then
    url="$(ping_url || true)"
    url="${url%/}"
    if [[ -n "$url" ]]; then
        {
            printf 'cron-run.sh : code de sortie %d\n\n' "$status"
            tail -c +"$((offset + 1))" "$LOG" | tail -c "$PING_BODY_MAX_BYTES"
        } | curl -fsS -m 10 --retry 2 --data-binary @- "$url/fail" >> "$LOG" 2>&1 \
            || printf 'cron-run.sh : ping /fail impossible\n' >> "$LOG"
    fi
fi
exit "$status"
