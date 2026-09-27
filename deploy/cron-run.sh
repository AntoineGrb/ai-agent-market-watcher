#!/usr/bin/env bash
# Run quotidien lancé par le cron de l'hôte (cadrage §11.2) :
#   0 7 * * * /opt/watcher/deploy/cron-run.sh
# Sans argument : commande par défaut du service (`python -m watcher.run --all`). Avec arguments : commande
# passée au conteneur (ex. `deploy/cron-run.sh python -m watcher.run --heartbeat`).
# -T : pas de pseudo-terminal (le cron n'en a pas). Sortie ajoutée à data/cron.log (rotation : logrotate).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
mkdir -p data
{
    printf '=== %s ===\n' "$(date -Iseconds)"
    docker compose run --rm -T watcher "$@"
} >> data/cron.log 2>&1
