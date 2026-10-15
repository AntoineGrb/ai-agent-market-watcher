# Raccourcis shell du serveur (docs/exploitation.md §17), à charger une fois pour toutes depuis ~/.bashrc :
#   echo '. /opt/watcher/deploy/aliases.sh' >> ~/.bashrc
# Versionnés avec le dépôt : un `git pull` les met à jour (nouveaux terminaux, ou `source ~/.bashrc`).
# Chemins absolus : les alias marchent depuis n'importe quel dossier. Aide-mémoire : whelp.

_watcher_dir="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"

alias w="cd $_watcher_dir"
alias wrun="docker compose -f $_watcher_dir/compose.yaml run --rm watcher python -m watcher.run"
alias wlog="tail -n 80 $_watcher_dir/data/prod/logs/watcher.log"
alias werr="grep -E 'ERROR|WARNING' $_watcher_dir/data/prod/logs/watcher.log"
alias wdb="sqlite3 -header -column $_watcher_dir/data/prod/watcher.sqlite"
alias wruns='wdb "select id, scope, status, started_at, agents_ok, agents_failed, alerts_created, input_tokens, output_tokens from runs order by id desc limit 10;"'
alias whelp="$_watcher_dir/deploy/aide.sh"
complete -W "run logs db config docker cron serveur backup env diag tout" whelp

unset _watcher_dir
