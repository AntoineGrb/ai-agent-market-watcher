#!/usr/bin/env bash
# Aide-mémoire des commandes du serveur dans le terminal : version courte de docs/exploitation.md, à tenir à
# jour avec lui (toute commande ajoutée ou modifiée là-bas est reportée ici).
# Usage : deploy/aide.sh [rubrique]   (alias whelp, voir deploy/aliases.sh)
# Sans argument : alias, essentiel et liste des rubriques ; `tout` : tout à la suite. Dans un terminal, titres
# en gras et pagination par less quand l'aide dépasse l'écran (q pour quitter).
set -eu

RUBRIQUES=(run logs db config docker cron serveur backup env diag)

aide_accueil() {
    cat <<'EOF'
## Watcher : aide-mémoire du serveur (version longue : docs/exploitation.md)
Commandes à lancer depuis /opt/watcher (alias w) ; les alias marchent depuis n'importe quel dossier.
⚠️  modifie quelque chose ou envoie de vrais mails    💰 consomme des crédits API Anthropic
Ne rien lancer entre 06:55 et 07:15 : c'est le créneau du run quotidien.

## Alias (deploy/aliases.sh, chargé par ~/.bashrc)
  w                     cd /opt/watcher
  wrun <options>        docker compose run --rm watcher python -m watcher.run <options>   (whelp run)
  wlog                  80 dernières lignes de data/prod/logs/watcher.log                 (whelp logs)
  werr                  erreurs et avertissements du jour dans ce journal
  wdb ["<requête>;"]    sqlite3 sur la base de prod ; sans requête : mode interactif      (whelp db)
  wruns                 10 derniers runs
  whelp [rubrique]      cette aide (Tab complète le nom de la rubrique)

## L'essentiel
  wruns                                             # le run de ce matin s'est-il bien passé ? (ou Healthchecks)
  werr                                              # erreurs et avertissements du jour
  ./deploy/cron-run.sh; tail -n 40 data/cron.log    # relancer le run du jour  ⚠️💰
  wrun --agent UBI                                  # un seul agent  ⚠️💰
  wrun --heartbeat                                  # recevoir le heartbeat maintenant  ⚠️
  wrun --test-mail                                  # tester l'envoi de mail  ⚠️
  git pull                                          # appliquer une modif de config / prompt (faite sur le PC)
  git pull && docker compose build && docker image prune -f    # appliquer une modif de code
  mkdir -p data/backups && sqlite3 data/prod/watcher.sqlite ".backup data/backups/watcher-$(date +%F).sqlite"
                                                    # sauvegarder la base
  crontab -l                                        # voir le cron
  df -h / && docker system df                       # place disque

## Rubriques : whelp <rubrique>
  run       options du watcher, statut d'un run
  logs      lire les logs
  db        requêtes SQLite prêtes à l'emploi, corrections manuelles
  config    appliquer une modification, vérifier les configs, nouvel agent
  docker    image, conteneurs, nettoyage
  cron      planification, pause, relance
  serveur   système, mises à jour, sécurité
  backup    sauvegarde, restauration, rapatriement sur le PC
  env       secrets (.env)
  diag      diagnostic par symptôme, services externes
  tout      toutes les rubriques à la suite
EOF
}

aide_run() {
    cat <<'EOF'
## Commandes du watcher : wrun <options>
wrun = docker compose run --rm watcher python -m watcher.run ; une cible est obligatoire (--all, --agent,
--heartbeat ou --test-mail). docker compose run --rm watcher, sans commande, lance le run quotidien --all.

  wrun --all                        # run quotidien, tous les agents ; heartbeat le lundi s'il n'est pas parti  ⚠️💰
  wrun --agent UBI                  # un seul agent (scope « agent UBI », sans ping Healthchecks)  ⚠️💰
  wrun --all --baseline             # marque tous les documents comme vus, initialise les références ; ni LLM ni mail
  wrun --agent UBI --baseline       # idem pour un agent (nouvel agent, reprise après une longue panne)
  wrun --heartbeat                  # heartbeat tout de suite, sans exécuter les agents  ⚠️
  wrun --test-mail                  # mail de test SMTP ; environnement test forcé, objet [TEST]  ⚠️
  wrun --agent NANO --inject fixtures/nano/<cas>.md              # document d'exemple dans tout le pipeline  ⚠️💰
  wrun --agent NANO --inject fixtures/nano/<cas>.md --primary    # idem, traité comme source primaire  ⚠️💰
  ls fixtures/nano fixtures/ubi     # cas disponibles pour --inject
  wrun --help                       # options brutes du watcher
  ./deploy/cron-run.sh              # exactement ce que lance le cron ; sortie dans data/cron.log  ⚠️💰
  ./deploy/cron-run.sh python -m watcher.run --heartbeat         # une autre commande via le script  ⚠️
  echo $?                           # code de sortie de la dernière commande : 0 OK, 1 échec, 2 usage

--primary est nécessaire pour voir une recommandation actionnable (SELL_ALL, SELL_HALF, BUY).
--inject et --test-mail forcent l'environnement de test (data/test/, objets [TEST]) : l'état de prod n'est pas
touché. Relancer un run le même jour est sans risque : documents déjà vus ignorés, alertes dédoublonnées.

## Statut d'un run (colonne runs.status)
  ok        tous les agents ont réussi (source ou cours en erreur = simple avertissement)
  partial   au moins un agent OK et quelque chose a échoué (agent, mail, config) ; Healthchecks reçoit /fail
  failed    erreur globale, budget de tokens épuisé, ou aucun agent OK
  running   run en cours, ou interrompu brutalement (serveur redémarré pendant le run)
EOF
}

aide_logs() {
    cat <<'EOF'
## Logs
data/prod/logs/watcher.log   journal applicatif, le premier endroit où regarder. Rotation à minuit : les jours
                             précédents deviennent watcher.log.AAAA-MM-JJ, 30 jours gardés.
data/cron.log                sortie brute de chaque lancement du cron, précédée de === date === ; utile si le
                             conteneur n'a pas démarré. Rotation hebdomadaire compressée, 8 semaines gardées.

  wlog                                                    # 80 dernières lignes
  tail -f data/prod/logs/watcher.log                      # suivre en direct (Ctrl+C pour quitter)
  less +G data/prod/logs/watcher.log                      # parcourir (q pour quitter, / pour chercher)
  werr                                                    # erreurs et avertissements du jour
  grep -E "ERROR|WARNING" data/prod/logs/watcher.log.*    # … des jours précédents
  grep "match" data/prod/logs/watcher.log                 # ce que l'analyse LLM a trouvé
  grep "retenu" data/prod/logs/watcher.log                # ce que le tri a gardé
  grep "outbox" data/prod/logs/watcher.log                # alertes écrites et mails envoyés
  grep "tokens" data/prod/logs/watcher.log                # consommation par appel LLM
  grep -E "run [0-9]+ : " data/prod/logs/watcher.log      # résumé de fin de chaque run
  grep "UBI" data/prod/logs/watcher.log                   # tout ce qui concerne un agent
  tail -n 60 data/cron.log                                # dernier lancement vu par le cron
  zgrep "Error" data/cron.log.*.gz                        # chercher dans les archives compressées
EOF
}

aide_db() {
    cat <<'EOF'
## Base SQLite : wdb = sqlite3 -header -column data/prod/watcher.sqlite
Dates en UTC (07:00 à Paris = 05:00 ou 06:00 UTC). sqlite3 est installé sur le serveur, pas dans le conteneur.

  wdb             # mode interactif : .tables, .schema runs, .quit ; chaque requête se termine par ;
  wruns           # 10 derniers runs

  # Derniers runs, avec durée
  wdb "select id, scope, status, started_at, round((julianday(finished_at) - julianday(started_at)) * 86400) as duree_s, agents_ok, agents_failed, alerts_created, input_tokens, output_tokens from runs order by id desc limit 15;"
  # Erreurs et avertissements des derniers runs non OK, puis du dernier run (même OK)
  wdb "select id, started_at, status, errors_json from runs where status != 'ok' order by id desc limit 5;"
  wdb "select errors_json from runs order by id desc limit 1;"
  # Dernières alertes (envoyées ou non)
  wdb "select id, agent_id, rule_id, origin, action, severity, is_rumor, event_date, substr(json_extract(payload_json, '$.headline'), 1, 60) as titre, sent_at from events order by id desc limit 20;"
  # Détail complet d'une alerte (remplacer 42 par son id)
  wdb "select json_extract(payload_json, '$.rationale') as analyse, json_extract(payload_json, '$.confidence') as confiance, json_extract(payload_json, '$.figures') as chiffres, json_extract(payload_json, '$.metrics') as metriques, json_extract(payload_json, '$.downgrade_reason') as retrogradation, json_extract(payload_json, '$.evidence') as preuves from events where id = 42;"
  # Boîte d'envoi : alertes pas encore envoyées (normalement vide après un run)
  wdb "select id, agent_id, rule_id, action, severity, created_at from events where sent_at is null;"
  # Surveillances armées actives
  wdb "select id, agent_id, arm_id, metric, ref_value, op, threshold, action, created_at from armed_watches where status = 'active';"
  # Règles déjà déclenchées par agent (ce qui alimente unless_fired)
  wdb "select agent_id, rule_id, count(*) as nb, max(created_at) as derniere from events group by agent_id, rule_id order by derniere desc;"
  # Documents retenus par le tri (les plus récents)
  wdb "select agent_id, source_name, substr(title, 1, 80) as titre, first_seen_at from seen_items where triage_relevant = 1 order by first_seen_at desc limit 30;"
  # Volume de documents par jour et par source
  wdb "select substr(first_seen_at, 1, 10) as jour, agent_id, source_name, count(*) as docs, sum(triage_relevant) as retenus from seen_items group by jour, agent_id, source_name order by jour desc limit 40;"
  # Tokens consommés par jour (le coût en dollars, par modèle, est dans le heartbeat)
  wdb "select substr(started_at, 1, 10) as jour, count(*) as runs, sum(input_tokens) as tokens_entree, sum(output_tokens) as tokens_sortie from runs group by jour order by jour desc limit 14;"
  # Erreurs de configuration en cours
  wdb "select agent_id, message, first_seen_at, notified_at from config_errors where resolved_at is null;"
  # État mémorisé des sources (dernière clôture traitée, photo ClinicalTrials)
  wdb "select agent_id, source_name, updated_at, substr(state_json, 1, 120) as etat from source_state;"
  # Heartbeats envoyés
  wdb "select * from heartbeats order by day desc limit 10;"

## Corrections manuelles (rare) ⚠️ : sauvegarde d'abord (whelp backup), hors créneau de 07:00
  # Annuler une surveillance armée (remplacer 3 par son id). Si la position est soldée, préférer
  # status: CLOSED dans la config : toutes les surveillances de l'agent sont annulées au run suivant.
  wdb "update armed_watches set status = 'cancelled', closed_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') where id = 3 and status = 'active';"
  # Ne jamais envoyer une alerte restée dans la boîte d'envoi (remplacer 42 par son id)
  wdb "update events set sent_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') where sent_at is null and id = 42;"
EOF
}

aide_config() {
    cat <<'EOF'
## Appliquer une modification : toujours faite sur le PC (tests, commit, push), jamais éditée ici
  git status                     # doit être « clean » ; sinon, quelqu'un a édité sur le serveur
  git pull                       # agents/, fixtures/, deploy/, compose.yaml, docs/ : pas de rebuild
  git pull && docker compose build && docker image prune -f    # watcher/, requirements.txt, Dockerfile : rebuild
  git log -3 --oneline           # vérifier que le dernier commit est bien là
  git diff                       # voir une modification locale faite sur le serveur
  git checkout -- <fichier>      # jeter la modification locale d'un fichier  ⚠️
  source ~/.bashrc               # recharger les alias après un git pull qui modifie deploy/aliases.sh

Pris en compte au prochain run (07:00), ou tout de suite en relançant un run à la main.
Code qui touche la base (watcher/store.py, migration) : sauvegarde avant le git pull (whelp backup).
Rebuild aussi si compose.yaml change de section build:. Secrets : éditer .env ici (whelp env).

## Vérifier les configs sans lancer de run (ni réseau, ni mail, ni coût) ; attendu : OK : ['NANO', 'UBI']
  docker compose run --rm watcher python -c "from pathlib import Path; from watcher.config import load_defaults, load_agents; d = Path('agents'); load_defaults(d); r = load_agents(d); print('OK :', sorted(r.configs)); [print('ERREUR', k, ':', v.message) for k, v in r.errors.items()]"
Couvre le schéma des configs et la présence des prompt.md, pas les paramètres propres à chaque source (validés
au run, avec un mail [CONFIG] en cas d'erreur).

## Nouvel agent : après le git pull, avant son premier run quotidien
  wrun --agent <ID> --baseline
EOF
}

aide_docker() {
    cat <<'EOF'
## Docker (aucun conteneur permanent : le cron en crée un chaque matin, supprimé à la fin)
  docker compose build                              # reconstruire l'image après une modif de code
  docker compose build --pull                       # idem avec la dernière image Python de base (mensuel)
  docker compose build --no-cache --pull            # reconstruire de zéro (problème de cache)
  docker image prune -f                             # supprimer les anciennes images inutilisées
  docker builder prune -f                           # vider le cache de construction
  docker system df                                  # place occupée par Docker
  docker images                                     # lister les images
  docker ps                                         # conteneurs en cours (normalement aucun hors 07:00)
  docker ps -a                                      # y compris arrêtés (normalement aucun grâce à --rm)
  docker stop <ID_DU_CONTENEUR>                     # arrêter un run bloqué (ID donné par docker ps)
  docker container prune -f                         # supprimer les conteneurs arrêtés oubliés
  docker compose run --rm watcher bash              # terminal dans un conteneur (exit pour sortir)
  docker compose run --rm watcher python --version  # version de Python de l'image
  docker compose run --rm watcher pip list          # versions des bibliothèques
  docker compose config --quiet && echo OK          # vérifier la syntaxe de compose.yaml
  systemctl status docker                           # état du service Docker

⚠️  docker compose config SANS --quiet affiche les secrets du .env : ne jamais copier sa sortie ailleurs.
EOF
}

aide_cron() {
    cat <<'EOF'
## Cron (utilisateur ubuntu, fuseau Europe/Paris) : 0 7 * * * /opt/watcher/deploy/cron-run.sh
  crontab -l                                        # afficher les tâches planifiées
  crontab -e                                        # modifier (nano : Ctrl+O enregistrer, Ctrl+X quitter)  ⚠️
  systemctl status cron                             # le cron tourne-t-il ?
  journalctl -u cron --since today | grep cron-run  # s'est-il déclenché ce matin ?
  tail -n 60 data/cron.log                          # ce que le run a affiché
  ./deploy/cron-run.sh                              # lancer exactement ce que lance le cron  ⚠️💰
  ./deploy/cron-run.sh python -m watcher.run --heartbeat    # autre commande, sortie dans cron.log  ⚠️

Format : minute heure jour-du-mois mois jour-de-la-semaine commande.
Pause ⚠️ : crontab -e, # devant la ligne, ET mettre le check en pause dans Healthchecks (sinon : panne signalée).
Reprise : retirer le #, réactiver le check Healthchecks.
EOF
}

aide_serveur() {
    cat <<'EOF'
## Serveur (système, sécurité)
  df -h /                                           # place disque
  du -sh /opt/watcher/data/*                        # taille des données du watcher
  free -h                                           # mémoire
  uptime                                            # charge et durée depuis le dernier démarrage
  timedatectl                                       # fuseau et heure du serveur
  sudo apt update && apt list --upgradable          # mises à jour disponibles
  sudo apt update && sudo apt upgrade -y            # les appliquer (la sécurité s'installe déjà seule)  ⚠️
  cat /var/run/reboot-required 2>/dev/null || echo "non"    # un redémarrage est-il requis ?
  sudo reboot                                       # hors créneau de 07:00 ; Docker et cron repartent seuls  ⚠️
  less /var/log/unattended-upgrades/unattended-upgrades.log # journal des mises à jour automatiques
  sudo ufw status verbose                           # pare-feu (attendu : entrant refusé sauf 22/tcp)
  sudo sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication) '    # attendu : no / no
  journalctl -u ssh --since "24 hours ago" | tail -n 30                   # tentatives de connexion SSH
  last -n 10                                        # connexions réussies récentes
  journalctl -p err --since today                   # erreurs système du jour
  sudo ./deploy/setup-vps.sh                        # relancer la préparation du serveur (idempotent)  ⚠️

Un port publié par Docker (ports: dans un compose.yaml) contourne ufw ; le watcher n'en publie aucun.
EOF
}

aide_backup() {
    cat <<'EOF'
## Sauvegarde (sûre même si un run tourne)
  mkdir -p data/backups && sqlite3 data/prod/watcher.sqlite ".backup data/backups/watcher-$(date +%F).sqlite"
  ls -lh data/backups
  sqlite3 data/backups/watcher-AAAA-MM-JJ.sqlite "pragma integrity_check;"    # vérifier (attendu : ok)
Rapatrier sur le PC (PowerShell) : le serveur n'est pas une sauvegarde en soi.
  scp "watcher:/opt/watcher/data/backups/*.sqlite" .

## Restaurer ⚠️ (hors créneau de 07:00)
  cp data/prod/watcher.sqlite data/prod/watcher.sqlite.avant-restauration
  cp data/backups/watcher-AAAA-MM-JJ.sqlite data/prod/watcher.sqlite
  sqlite3 data/prod/watcher.sqlite "pragma integrity_check;"                  # attendu : ok

## Sauvegarde hebdomadaire automatique (optionnel) : ligne à ajouter avec crontab -e
Dimanche 06:30, 60 jours conservés ; dans une crontab, % s'écrit \%.
  30 6 * * 0 mkdir -p /opt/watcher/data/backups && sqlite3 /opt/watcher/data/prod/watcher.sqlite ".backup /opt/watcher/data/backups/watcher-$(date +\%F).sqlite" && find /opt/watcher/data/backups -name 'watcher-*.sqlite' -mtime +60 -delete

Hors de la base : .env (garder ses valeurs dans un gestionnaire de mots de passe) et logs (non critiques).
EOF
}

aide_env() {
    cat <<'EOF'
## Secrets : /opt/watcher/.env (ni dans git, ni dans l'image), pris en compte au run suivant sans rebuild
  nano .env                                         # éditer (Ctrl+O enregistrer, Ctrl+X quitter)  ⚠️
  ls -l .env                                        # attendu : -rw------- ubuntu ubuntu
  chmod 600 .env                                    # rétablir les droits si besoin
  grep -E "^WATCHER_ENV|^WATCHER_MODEL" .env        # voir des valeurs non secrètes
Valeur contenant $ : l'entourer de guillemets simples (CLE='abc$def').

  WATCHER_ENV                          prod ou test
  ANTHROPIC_API_KEY                    clé API Claude (révoquée, compromise ou erreur 401 : la changer)
  WATCHER_MODEL_TRIAGE / _ANALYSIS     surcharge des modèles de _defaults.yaml (vide = pas de surcharge)
  SMTP_USER, SMTP_APP_PASSWORD         Gmail, mot de passe d'application (erreur 535 : le régénérer)
  MAIL_TO                              destinataire des mails
  HEALTHCHECKS_URL                     check de prod
  HEALTHCHECKS_TEST_URL                check de test (pingé seulement en test)
  SEC_USER_AGENT                       contact exigé par la SEC
  WATCHER_UID                          UID du propriétaire de data/ si différent de 1000 (puis docker compose build)

Clé qui a fuité : la révoquer chez le fournisseur (console Anthropic, compte Google), en créer une nouvelle,
mettre à jour .env, puis wrun --test-mail pour valider le SMTP.
EOF
}

aide_diag() {
    cat <<'EOF'
## Diagnostic rapide (symptôme, premières commandes, piste)
Healthchecks DOWN, rien ce matin dans data/cron.log
  crontab -l ; systemctl status cron ; journalctl -u cron --since today
  → cron absent ou arrêté, serveur redémarré pendant le créneau, serveur éteint (espace client OVH)
Healthchecks DOWN, erreur Docker dans data/cron.log
  tail -n 60 data/cron.log ; systemctl status docker ; df -h /
  → Docker arrêté, disque plein, .env absent
Healthchecks en échec (/fail)
  corps du ping dans Healthchecks ; werr ; erreurs des derniers runs non OK (whelp db)
  → config invalide, API LLM, SMTP, budget de tokens
Mail [CONFIG]
  lire le message ; vérifier les configs (whelp config)
  → clé mal orthographiée, valeur manquante (entry_price en OWNED…)
authentication_error / 401 Anthropic
  → console Anthropic : clé révoquée ou crédits épuisés
535 / Username and Password not accepted
  wrun --test-mail
  → mot de passe d'application Gmail à régénérer (whelp env)
Alertes créées mais pas de mail
  boîte d'envoi (whelp db) ; grep outbox data/prod/logs/watcher.log
  → échec SMTP : elles repartiront au run suivant
Plus aucun document pour une source
  grep "source" data/prod/logs/watcher.log ; heartbeat
  → source en erreur (site modifié, blocage) : docs/sources.md
Avertissement « cours »
  grep -i cours data/prod/logs/watcher.log
  → Yahoo bloqué : repli Euronext automatique ; si les deux échouent, règles de cours sautées ce jour-là
PermissionError sur /app/data/…
  ls -ln data ; id -u
  → sudo chown -R ubuntu:ubuntu data, ou WATCHER_UID dans .env puis docker compose build
Run resté en running
  docker ps ; wruns
  → run interrompu (redémarrage) ou bloqué : docker stop <ID>, puis ./deploy/cron-run.sh

## Services externes
  healthchecks.io                      état du check watcher-prod, historique et corps des pings, pause
  console.anthropic.com                consommation et coût réels, crédits, limite de dépense, clés API
  myaccount.google.com/apppasswords    mots de passe d'application Gmail (révoquer, régénérer)
  ovhcloud.com (espace client)         facturation, redémarrage ou console de secours du VPS
  github.com                           dépôt ; clé de déploiement du VPS (Settings → Deploy keys)
EOF
}

# Titres (lignes « ## ») en gras et pagination dans un terminal ; texte brut sinon (ex. redirection).
afficher() {
    if [[ -t 1 ]] && command -v less >/dev/null; then
        local gras=$'\e[1m' normal=$'\e[0m'
        sed "s/^## \(.*\)/$gras\1$normal/" | LESSCHARSET=utf-8 less -FRX
    else
        sed 's/^## //'
    fi
}

case "${1:-}" in
    ""|-h|--help|help) pages=(accueil) ;;
    tout) pages=(accueil "${RUBRIQUES[@]}") ;;
    *)
        if [[ " ${RUBRIQUES[*]} " != *" $1 "* ]]; then
            printf 'rubrique inconnue : %s\nrubriques : %s tout\n' "$1" "${RUBRIQUES[*]}" >&2
            exit 2
        fi
        pages=("$1")
        ;;
esac

for page in "${pages[@]}"; do
    [[ "$page" == "${pages[0]}" ]] || echo
    "aide_$page"
done | afficher
