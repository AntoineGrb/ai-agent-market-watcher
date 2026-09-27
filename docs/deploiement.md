# Déploiement sur le VPS (étape 6) : pas à pas

> Livrable de l'étape 6 du cadrage (§11, §13). Rédigé le 27/09/2026.
> Critère de fin : **7 runs consécutifs OK dans Healthchecks** en `WATCHER_ENV=test`, **heartbeat reçu**, puis bascule en prod après un `--baseline`.

## Ce qui est déjà prêt dans le dépôt

| Fichier | Rôle |
|---|---|
| `Dockerfile` | Image Python 3.12 slim, utilisateur non root (UID réglable via `WATCHER_UID`, 1000 par défaut), commande par défaut `python -m watcher.run --all`. |
| `.dockerignore` | Garde `.env`, `data/`, `.venv/`… hors du contexte de build. |
| `compose.yaml` | Service `watcher` : `.env` en `env_file`, `TZ=Europe/Paris`, volumes `data/` (lecture-écriture), `agents/` et `fixtures/` (lecture seule). Aucun port exposé. |
| `deploy/setup-vps.sh` | Préparation du VPS en une commande : fuseau, mises à jour auto, Docker + Compose, pare-feu, durcissement SSH, droits de `data/` et `.env`, rotation de `data/cron.log`. Idempotent. |
| `deploy/cron-run.sh` | Ce que lance le cron : `docker compose run --rm -T watcher`, sortie ajoutée à `data/cron.log`. |
| `HEALTHCHECKS_TEST_URL` | Nouvelle variable : check Healthchecks **dédié à l'environnement de test** (cadrage §11.4). En `WATCHER_ENV=test`, seul ce check est pingé, jamais celui de prod. |

Ce qui reste pour toi : les comptes, le VPS, les secrets, la semaine de rodage et la bascule. C'est l'objet de la suite.

## Calendrier conseillé

| Jour | Action |
|---|---|
| J0 | Étapes 1 à 9 : comptes, VPS, installation, validations manuelles, cron en `test`. |
| J1 → J7 | Semaine de rodage en `test` (étape 10). Le lundi, heartbeat `[TEST]`. |
| J8 | Bascule en prod (étape 11), **après** le run de 07:00 et **avant** celui du lendemain. |

---

## 1. Comptes et secrets à préparer

Coche au fur et à mesure. Garde les valeurs dans un gestionnaire de mots de passe, jamais dans git.

- [ ] **Clé API Anthropic** : [console.anthropic.com](https://console.anthropic.com) → *API Keys* → *Create key*. La facturation est distincte de l'abonnement Claude : ajoute des crédits (*Billing*) et fixe une **limite de dépense mensuelle** (quelques dollars suffisent, cadrage §7.5). → `ANTHROPIC_API_KEY`
- [ ] **Mot de passe d'application Gmail** : la validation en deux étapes doit être active sur le compte, puis [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) → nom « watcher » → 16 caractères (colle-les sans espaces). → `SMTP_USER` (l'adresse Gmail), `SMTP_APP_PASSWORD`, `MAIL_TO` (destinataire des alertes).
- [ ] **Healthchecks.io** : crée un compte, puis **deux checks** :
  - `watcher-test` et `watcher-prod`, chacun avec *Schedule* → **Cron**, expression `0 7 * * *`, *Time zone* `Europe/Paris`, *Grace time* **2 heures** ;
  - dans *Integrations*, vérifie que l'alerte par e-mail est active (c'est elle qui te préviendra d'une panne silencieuse).
  - Copie l'URL de ping de chaque check (`https://hc-ping.com/<uuid>`, sans suffixe). → `HEALTHCHECKS_TEST_URL`, `HEALTHCHECKS_URL`
- [ ] **User-Agent SEC** : texte libre avec un contact, ex. `watcher-perso antoine.xxx@exemple.com` (politique SEC, obligatoire pour la source EDGAR). → `SEC_USER_AGENT`
- [ ] **VPS OVHcloud** : l'offre d'entrée de gamme suffit (le job tourne quelques minutes par jour). Image **Ubuntu 24.04 LTS**. Si le formulaire de commande propose d'ajouter une clé SSH, fais l'étape 2 avant de commander.

## 2. Clé SSH sur ton PC (Windows, PowerShell)

```powershell
ssh-keygen -t ed25519 -C "watcher-vps"          # Entrée pour le chemin par défaut ; mets une passphrase
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub  # clé publique à coller chez OVH
```

- Si tu as ajouté la clé à la commande du VPS : rien d'autre à faire.
- Sinon (VPS livré avec un mot de passe par e-mail), installe la clé depuis PowerShell :

```powershell
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub | ssh ubuntu@<IP_DU_VPS> "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Optionnel mais pratique : un alias dans `C:\Users\<toi>\.ssh\config` pour taper `ssh watcher` :

```text
Host watcher
    HostName <IP_DU_VPS>
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
```

Vérifie : `ssh watcher` doit se connecter **sans demander le mot de passe du VPS** (seulement la passphrase de ta clé).

> Sur les VPS OVHcloud Ubuntu, l'utilisateur par défaut est `ubuntu` (UID 1000, sudo). Il sert d'utilisateur de déploiement dans ce guide. Si tu en utilises un autre, adapte `ubuntu` partout.

## 3. Pousser l'étape 6 sur GitHub

Depuis le PC, dans le dépôt : relis le diff, puis commit et push sur `main` (ou via une PR, comme pour l'étape 3). Le VPS clone depuis GitHub.

## 4. Cloner le dépôt sur le VPS

```bash
ssh watcher
sudo mkdir -p /opt/watcher && sudo chown ubuntu:ubuntu /opt/watcher
```

**Dépôt public** :

```bash
git clone https://github.com/AntoineGrb/ai-agent-market-watcher.git /opt/watcher
```

**Dépôt privé** : utilise une *deploy key* en lecture seule, propre au VPS.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/watcher_deploy -N "" -C "vps-watcher-deploy"
cat ~/.ssh/watcher_deploy.pub
```

Sur GitHub : dépôt → *Settings* → *Deploy keys* → *Add deploy key* → colle la clé, **sans** cocher *Allow write access*. Puis :

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/watcher_deploy
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
git clone git@github.com:AntoineGrb/ai-agent-market-watcher.git /opt/watcher
```

## 5. Préparer le système

```bash
cd /opt/watcher
sudo ./deploy/setup-vps.sh
```

Le script affiche chaque étape (`==> ...`). Points à vérifier dans la sortie :

- `Status: active` pour ufw, avec `22/tcp (OpenSSH) ALLOW` ;
- `permitrootlogin no`, `passwordauthentication no` ;
- si le message « authorized_keys vide ou absent » apparaît, le durcissement SSH a été sauté : installe ta clé (étape 2) et relance le script ;
- si un message te demande d'ajouter `WATCHER_UID=...` dans `.env`, fais-le à l'étape 6.

Puis, **sans fermer ta session actuelle**, ouvre un second terminal sur le PC :

```powershell
ssh watcher              # doit fonctionner : ta clé est acceptée
ssh root@<IP_DU_VPS>     # doit être refusé (Permission denied)
```

Si la connexion par clé échoue, corrige depuis la première session (toujours ouverte) avant de la fermer.

Enfin, déconnecte-toi et reconnecte-toi (appartenance au groupe `docker`), et vérifie :

```bash
docker compose version
timedatectl | grep "Time zone"     # Europe/Paris
```

## 6. Créer le fichier `.env` sur le VPS

Le plus simple : partir de ton `.env` local, qui contient déjà les clés. Depuis PowerShell, dans le dépôt :

```powershell
scp .env watcher:/opt/watcher/.env
```

Puis sur le VPS, édite-le (`nano /opt/watcher/.env`) pour obtenir :

```dotenv
WATCHER_ENV=test                         # semaine de rodage ; prod à l'étape 11
ANTHROPIC_API_KEY=sk-ant-...
WATCHER_MODEL_TRIAGE=
WATCHER_MODEL_ANALYSIS=
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=ton.adresse@gmail.com
SMTP_APP_PASSWORD=xxxxxxxxxxxxxxxx
MAIL_TO=ton.adresse@...
HEALTHCHECKS_URL=https://hc-ping.com/<uuid-watcher-prod>
HEALTHCHECKS_TEST_URL=https://hc-ping.com/<uuid-watcher-test>
SEC_USER_AGENT=watcher-perso ton.adresse@...
WATCHER_UID=                             # seulement si le script l'a demandé
```

```bash
chmod 600 /opt/watcher/.env
```

> Docker Compose interprète `$` dans les valeurs de `.env`. Si un secret contient `$`, entoure la valeur de guillemets simples : `CLE='abc$def'`.

## 7. Construire l'image

```bash
cd /opt/watcher
docker compose build
docker compose run --rm watcher python -c "import watcher, sys; print(sys.version)"
```

La deuxième commande doit afficher `3.12.x`. Le build prend quelques minutes la première fois.

## 8. Validations manuelles (en `test`)

Toutes les commandes se lancent depuis `/opt/watcher`. Chacune doit se terminer sans erreur ; vérifie le code de sortie avec `echo $?` (`0` = OK).

**a. SMTP** : un mail `[TEST]` doit arriver.

```bash
docker compose run --rm watcher python -m watcher.run --test-mail
```

**b. Baseline de l'environnement de test** : marque tous les documents actuels comme vus, initialise les instantanés (ClinicalTrials, dernière clôture). Aucun appel LLM, aucun mail.

```bash
docker compose run --rm watcher python -m watcher.run --all --baseline
```

Dans la sortie, cherche les avertissements (`WARNING`) : une source ou le cours en erreur n'empêchent pas le run, mais signalent un problème depuis l'IP du VPS. Rappel de `docs/sources.md` : yfinance peut être bloqué depuis certaines IP de datacenter, le repli CSV Euronext prend alors le relais pour UBI et NANO. Ce run pingue déjà le check `watcher-test`.

**c. Injection de bout en bout** : un document passe par le tri, l'analyse, la résolution et l'envoi. Un vrai mail `[TEST]` arrive. **Consomme quelques centimes d'API.**

```bash
docker compose run --rm watcher python -m watcher.run --agent NANO --inject fixtures/nano/endpoint_met.md --primary
```

Attendu : alerte `N-B1`. Autres cas disponibles dans `fixtures/nano/` et `fixtures/ubi/` (ex. `fixtures/ubi/takeover_offer.md`).

**d. Heartbeat** : mail `[TEST] [HEARTBEAT] ...` immédiat, sans exécuter les agents.

```bash
docker compose run --rm watcher python -m watcher.run --heartbeat
```

**e. Le script du cron**, exactement comme le lancera le cron :

```bash
./deploy/cron-run.sh
echo $?
tail -n 30 data/cron.log
```

Dans Healthchecks, le check `watcher-test` doit passer au vert, avec la durée du run et le résumé en corps du ping.

## 9. Installer le cron

```bash
crontab -e      # choisis nano si on te demande un éditeur
```

Ajoute la ligne :

```text
0 7 * * * /opt/watcher/deploy/cron-run.sh
```

Vérifie avec `crontab -l`. Le cron utilise le fuseau du système (Europe/Paris, réglé par le script).

## 10. Semaine de rodage en `test`

Chaque jour, vers 07:15 :

- Healthchecks : `watcher-test` vert. En cas d'échec, le corps du ping `/fail` contient le résumé des erreurs.
- Boîte mail : alertes `[TEST]` éventuelles. Le silence est le cas normal.

Pour inspecter depuis le VPS :

```bash
cd /opt/watcher
tail -n 50 data/test/logs/watcher.log
sqlite3 data/test/watcher.sqlite "select id, scope, status, started_at, finished_at from runs order by id desc limit 10;"
```

Critère : **7 runs `all` consécutifs en `ok`**, et le **heartbeat du lundi** reçu (`[TEST] [HEARTBEAT] Semaine du JJ/MM — 7/7 runs OK, ...`).

Si un run échoue : lis l'erreur (Healthchecks, `watcher.log`), corrige, puis recompte 7 runs à partir du correctif.

## 11. Bascule en prod

À faire **après** le run de 07:00 de J8 et **avant** 07:00 le lendemain.

1. **Vérifie les configs** (`agents/*/config.yaml`) : statut, `entry_price`, `entry_date`, `shares_outstanding`. Modifie-les sur ton PC, commit, push, puis sur le VPS :
   ```bash
   cd /opt/watcher && git pull
   ```
   `agents/` est monté dans le conteneur : pas besoin de reconstruire l'image.
2. **Passe en prod** : dans `/opt/watcher/.env`, remplace `WATCHER_ENV=test` par `WATCHER_ENV=prod`.
3. **Baseline de prod** (base neuve `data/prod/watcher.sqlite`). Sans elle, le premier run de prod analyserait les documents déjà présents dans les flux :
   ```bash
   docker compose run --rm watcher python -m watcher.run --all --baseline
   ```
   Ce run pingue `watcher-prod` : il doit passer au vert.
4. **Healthchecks** : mets le check `watcher-test` en pause (*Pause*) ou supprime-le, sinon il te signalera une panne dans 26 h.
5. Le lendemain : `watcher-prod` vert après 07:00. Le premier heartbeat de prod arrive le lundi suivant.

La base de test (`data/test/`) reste en place et ne gêne pas. Les commandes `--inject` et `--test-mail` forcent toujours l'environnement de test : tu peux continuer à les utiliser en prod sans polluer l'état.

---

## Exploitation courante

| Besoin | Commande (depuis `/opt/watcher`) |
|---|---|
| Changer une position, une règle, un prompt | Modifier sur le PC → commit / push → `git pull` sur le VPS. Pas de rebuild. Évite d'éditer directement sur le VPS (conflits au prochain `git pull`). |
| Mettre à jour le code | `git pull && docker compose build`, puis `docker image prune -f` pour libérer l'ancienne image. |
| Relancer le run du jour à la main | `./deploy/cron-run.sh` (le dédoublonnage évite les alertes en double). |
| Un seul agent | `docker compose run --rm watcher python -m watcher.run --agent UBI` |
| Heartbeat tout de suite | `docker compose run --rm watcher python -m watcher.run --heartbeat` |
| Logs applicatifs | `data/<env>/logs/watcher.log` (rotation quotidienne, 30 jours) |
| Sortie brute du cron | `data/cron.log` (rotation hebdomadaire, 8 semaines) |
| Sauvegarde de la base | `sqlite3 data/prod/watcher.sqlite ".backup data/backup-$(date +%F).sqlite"`, puis depuis le PC : `scp watcher:/opt/watcher/data/backup-*.sqlite .` |
| Ajouter un agent | Nouveau dossier `agents/<id>/` (config + prompt), push, `git pull`. Penser à `--agent <ID> --baseline` avant son premier run. |
| Changer un secret | Éditer `.env` sur le VPS. Pris en compte au prochain run. |

## Dépannage

| Symptôme | Cause probable | Correction |
|---|---|---|
| `permission denied while trying to connect to the Docker daemon socket` | Groupe `docker` pas encore pris en compte | Se déconnecter / reconnecter. |
| `PermissionError` sur `/app/data/...` | `data/` n'appartient pas à l'UID du conteneur | `sudo chown -R ubuntu:ubuntu data`, ou `WATCHER_UID=$(id -u)` dans `.env` puis `docker compose build`. |
| `the input device is not a TTY` dans `cron.log` | `-T` absent | Passer par `deploy/cron-run.sh`, qui l'inclut. |
| `env file .../.env not found` | `.env` absent ou mal placé | Il doit être dans `/opt/watcher/.env`. |
| Mail non envoyé, `535` / `Username and Password not accepted` | Mot de passe Gmail classique au lieu du mot de passe d'application, ou 2FA désactivée | Régénérer le mot de passe d'application (étape 1). |
| Healthchecks ne reçoit rien alors que le run tourne | URL du mauvais environnement | En `test`, seul `HEALTHCHECKS_TEST_URL` est pingé ; en `prod`, seul `HEALTHCHECKS_URL`. Les runs `--agent` et `--inject` ne pinguent jamais. |
| Healthchecks en retard, rien dans `cron.log` | Cron non déclenché | `crontab -l`, `systemctl status cron`, `journalctl -u cron --since today`. |
| Avertissement cours dans le heartbeat | yfinance bloqué ou en panne | Repli Euronext automatique pour Euronext Paris ; si les deux échouent, règles de prix et anomalie sautées ce jour-là (cadrage §8.3). |
| Run en échec avec `_budget` | Plafond `llm_budget.max_total_tokens_per_run` atteint | Regarder quel agent a reçu beaucoup de documents (`watcher.log`) ; ajuster le plafond dans `_defaults.yaml` si c'est légitime. |
| Erreur `401` / `authentication_error` Anthropic | Clé révoquée ou crédits épuisés | Console Anthropic : clé et solde. |
| VPS arrêté plusieurs jours | Documents plus anciens que `max_item_age_days` (3 j) manqués | Limitation assumée (cadrage §14) : élargir temporairement `max_item_age_days` pour un run manuel. |
