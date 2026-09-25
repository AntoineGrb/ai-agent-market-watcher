# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## État du dépôt

Étape 0 (spike sources) terminée : voir `docs/sources.md` (endpoints validés, pièges, points ouverts) et les réponses réelles enregistrées dans `tests/data/sources/`. Le package `watcher/` est encore vide ; l'arborescence cible est décrite dans `.claude/docs/cadrage.md` §3.2.

Mise en place locale (Windows) : `python -m venv .venv` puis `.venv/Scripts/python -m pip install -r requirements-dev.txt`. `pytest` exclut le marqueur `eval` par défaut (`pyproject.toml`). Sur ce poste, Avast intercepte le TLS : les appels réseau Python échouent sans bundle CA adapté (voir `docs/sources.md` §8), ne rien contourner dans le code.

## Documents de référence

- **`.claude/docs/cadrage.md`** : source de vérité (architecture, schémas Pydantic, moteur de règles, SQLite, plan d'étapes). Le lire en entier avant d'écrire du code.
  - Les décisions de la §2 sont **actées** : ne pas les rediscuter, signaler seulement une impossibilité technique.
  - Les éléments marqués 🔲 ne doivent **pas** être inventés : valeur `null` ou source en `enabled: false`.
  - Implémenter étape par étape (§13, étapes 0 à 6) ; ne pas passer à l'étape suivante sans tests verts.
- **`docs/sources.md`** : livrable de l'étape 0, référence pour implémenter les fetchers et le provider de cours (ex. `when:Nd` obligatoire sur Google News, AMF via l'API info-financiere.gouv.fr filtrée par ISIN, barre du jour à écarter dans les cours).
- **`.claude/docs/specs.md`** (v1.2) : ne sert plus que de source du **contenu rédactionnel** des `agents/<id>/prompt.md` (thèse, contexte, nuances). Ses tableaux de règles, sa section 0 et ses références à `watch_rules.yaml` / `watch_models.py` sont obsolètes : les règles vivent dans `agents/<id>/config.yaml`.

## Commandes prévues (cadrage §11–12)

```bash
python -m watcher.run --all                  # run quotidien
python -m watcher.run --agent NANO           # un seul agent
python -m watcher.run --all --baseline       # premier démarrage : marque tout comme vu, sans LLM ni mail
python -m watcher.run --agent NANO --inject fixtures/nano/<cas>.md --primary   # bout en bout, force WATCHER_ENV=test
pytest                                       # tests unitaires (sans réseau ni LLM)
pytest tests/test_x.py::test_y               # un seul test
pytest -m eval                               # evals avec appels LLM réels (à la demande)
docker compose run --rm watcher              # exécution conteneurisée (cron hôte)
```

Stack : Python 3.12, `pydantic-ai-slim[anthropic]` (versions épinglées dans `requirements.txt` ; vérifier les signatures dans la doc de la version installée), Pydantic v2, `httpx`, `feedparser`, `PyYAML` (`safe_load` uniquement), `yfinance` derrière l'interface `PriceProvider`, stdlib pour `sqlite3` / `smtplib` / `logging`.

## Architecture (vue d'ensemble)

**Machine à agents** : un agent = un dossier `agents/<id>/` (`config.yaml` + `prompt.md`). Ajouter une société ne doit nécessiter aucune modification de code tant que les types de sources existent. `agents/_defaults.yaml` porte les paramètres globaux.

**Principe central — répartition LLM / code** :
- Le LLM **identifie** des événements (`rule_id`) et **extrait des chiffres bruts**, avec preuves par IDs de documents (`RuleMatch`). Il ne décide jamais d'action ni de sévérité et ne fait aucun calcul.
- Le **code** construit l'`Alert` : action, sévérité, métriques (dilution, primes, `price_vs_entry`…), seuils, dates. Tout est dans `watcher/engine/`.

**Pipeline d'un run** (cadrage §3.1) : config validée agent par agent → cours → fetch des sources (plugins) → tri (Haiku, orienté rappel) → analyse (Sonnet, `output_validator` + `ModelRetry` sur `rule_id`/`item_id` inconnus) → résolution (overrides, garde-fou actionnable, rumeurs, armement, contradictions) → règles déterministes (`price`, surveillances armées, `time`, `P-ANOMALY`) → dédoublonnage + priorité → outbox SQLite → envoi SMTP → Healthchecks.

Invariants à respecter :
- **Isolation par agent** : une exception ou une config invalide n'arrête jamais les autres agents. L'état (`seen_items`, `armed_watches`, `events`) n'est commité que si l'agent a réussi.
- **Outbox** : alertes écrites avec `sent_at = NULL`, renseigné seulement après succès SMTP ; les non-envoyées repartent au run suivant.
- **Garde-fous** : une action actionnable (`SELL_ALL`, `SELL_HALF`, `BUY`) exige une source primaire et `confidence ≥ 0,8`, sinon `RECO_UNCLEAR`. Une rumeur (toutes preuves `primary: false`) n'est jamais CRITICAL. `source_primary` vient de la config, jamais du LLM.
- **Ne jamais forcer un retry sur un chiffre manquant** : le validateur ne vérifie pas `extracted_figures` (sinon le modèle invente). Chiffre absent → `MetricUnavailable` → `missing_figures` (`RECO_UNCLEAR`).
- Le modèle LLM est passé au moment du run (config / `WATCHER_MODEL_TRIAGE`, `WATCHER_MODEL_ANALYSIS`), pas à la construction de l'agent.
- Types de règles : `event` (LLM, avec `overrides` et `arms`), `price` et `time` (code). `unless_fired` se base sur l'historique complet de la table `events`.
- Fetchers : registre par `type` (Protocol `Fetcher` dans `sources/base.py`) ; une source en erreur est loggée mais ne fait pas échouer l'agent.

**Environnements** : `WATCHER_ENV=prod|test`. Le test utilise `data/test/watcher.sqlite` et préfixe les objets de mail par `[TEST]`. **Pas de dry-run** : les mails de test partent réellement.

**Périmètre** : strictement consultatif — aucune exécution d'ordre ni accès courtier. Statut, prix et date d'entrée sont saisis à la main dans la config.

## Tests

- Tests unitaires sans réseau ni LLM ; dates **injectées** (jamais `date.today()` en dur) ; fetchers testés sur réponses enregistrées dans `tests/data/` ; plomberie LLM via `TestModel` / `FunctionModel` de PydanticAI.
- Objectif de couverture ≥ 90 % sur `watcher/engine/`.
- Evals : fixtures `fixtures/<agent>/<cas>.md` avec en-tête YAML (`expected_rule_ids`, `expected_figures`, `synthetic`). Critères : zéro faux négatif sur les `RECO_SELL_ALL` primaires, zéro faux positif de vente, chiffres exacts.
