# Guide du projet Watcher

> Rédigé le 27/09/2026, à la fin de la phase 1 (étapes 0 à 6).
> Public : toi, sans prérequis technique. Objectif : comprendre ce que fait chaque pièce du projet et
> **maîtriser la couche LLM** pour pouvoir la réutiliser ailleurs.
> Documents de référence plus techniques : `cadrage.md` (la spécification), `sources.md` (les sources de données),
> `deploiement.md` (la mise en production), `exploitation.md` (les commandes du quotidien).

---

## Sommaire

1. [Le projet en deux minutes](#1-le-projet-en-deux-minutes)
2. [Une journée type : le run de 07:00](#2-une-journée-type--le-run-de-0700)
3. [Les cinq idées qui structurent tout le projet](#3-les-cinq-idées-qui-structurent-tout-le-projet)
4. [Visite guidée du dépôt, dossier par dossier](#4-visite-guidée-du-dépôt-dossier-par-dossier)
5. [Anatomie d'un agent : `config.yaml` et `prompt.md`](#5-anatomie-dun-agent--configyaml-et-promptmd)
6. [La couche LLM en profondeur](#6-la-couche-llm-en-profondeur)
7. [Le moteur de règles : là où se prennent les décisions](#7-le-moteur-de-règles--là-où-se-prennent-les-décisions)
8. [Sources de données et cours de bourse](#8-sources-de-données-et-cours-de-bourse)
9. [La mémoire du système : la base SQLite](#9-la-mémoire-du-système--la-base-sqlite)
10. [Mails et surveillance du bon fonctionnement](#10-mails-et-surveillance-du-bon-fonctionnement)
11. [Tests : comment on sait que ça marche](#11-tests--comment-on-sait-que-ça-marche)
12. [Déploiement : conteneur, VPS, cron](#12-déploiement--conteneur-vps-cron)
13. [Ce que tu peux réutiliser dans d'autres projets LLM](#13-ce-que-tu-peux-réutiliser-dans-dautres-projets-llm)
14. [Glossaire](#14-glossaire)

---

## 1. Le projet en deux minutes

**Watcher** est un programme qui tourne seul, une fois par jour à 07:00, sur un petit serveur loué (VPS). Il
surveille des actions en bourse que tu détiens ou que tu envisages d'acheter (aujourd'hui **Ubisoft** et
**Nanobiotix**). Pour chacune :

1. il lit les **actualités** (communiqués officiels, presse, registre des essais cliniques…) ;
2. il récupère le **cours de bourse** ;
3. il se demande : « est-ce qu'une des règles que l'utilisateur a écrites est déclenchée ? » ;
4. si oui, il t'envoie **un mail de recommandation** (« vendre toute la ligne », « vendre la moitié »,
   « situation ambiguë, lis la source »…) ; sinon, il **se tait**.

Le silence est le cas normal. Pour que tu saches que le silence veut dire « rien à signaler » et non « le
programme est en panne », un service externe (**Healthchecks.io**) reçoit un signal à chaque exécution et te
prévient s'il n'en reçoit pas. Un mail récapitulatif (**heartbeat**) arrive aussi chaque lundi.

Ce que le projet ne fait **jamais** : passer un ordre, se connecter à ton courtier ou lire ton portefeuille. Il est
100 % consultatif. Ta position (acheté ou non, à quel prix, à quelle date) est écrite à la main dans un fichier de
configuration.

### Pourquoi un LLM ?

Les règles sont du type « si Nanobiotix annonce que son étude de phase 3 a atteint son critère principal, alors… ».
Aucun programme classique ne sait lire un communiqué de presse en anglais et reconnaître cette situation. Un LLM
(Claude) sait le faire. Mais un LLM peut aussi se tromper, inventer un chiffre ou varier d'un jour à l'autre. Tout
l'enjeu du projet est donc de **lui confier uniquement ce qu'il fait bien (lire et reconnaître) et de confier au
code tout le reste (calculer, décider, trier, envoyer)**. C'est l'idée centrale, détaillée en section 6.

---

## 2. Une journée type : le run de 07:00

Voici ce qui se passe, dans l'ordre, chaque matin. Un « run » est une exécution complète.

```
07:00  Le cron du serveur lance le conteneur Docker          (deploy/cron-run.sh)
        │
        ├─ Signal « je démarre » à Healthchecks
        │
        ├─ Pour chaque agent (UBI, puis NANO), de façon isolée :
        │    1. Lire et valider sa configuration              (config.py)
        │    2. Récupérer les ~15 derniers cours de clôture   (prices.py)
        │    3. Récupérer les nouveaux documents des sources  (sources/, ingest.py)
        │       → écarter ceux déjà vus et ceux de plus de 3 jours
        │    4. TRI par un petit modèle (Claude Haiku)        (llm/triage.py)
        │       → « lesquels de ces 40 titres méritent d'être lus ? »
        │    5. ANALYSE par un modèle plus fort (Claude Sonnet) (llm/analysis.py)
        │       → « ce document correspond-il à une règle ? preuves ? chiffres ? »
        │    6. DÉCISION par le code                          (engine/)
        │       → action, gravité, calculs, garde-fous, règles de cours et de date
        │    7. Dédoublonnage, puis écriture des alertes dans une « boîte d'envoi »
        │    8. Enregistrement de ce qui a été vu (seulement si tout s'est bien passé)
        │
        ├─ Envoi des mails : 1 par agent pour les alertes importantes, 1 digest pour le reste
        ├─ Le lundi : mail heartbeat
        └─ Signal « terminé : OK » ou « terminé : échec » à Healthchecks
07:0x  Le conteneur s'arrête et disparaît. Rien ne tourne jusqu'au lendemain.
```

La plupart des jours, l'étape 3 ne trouve que quelques articles, le tri n'en garde aucun ou presque, et l'analyse
ne trouve rien : aucun mail, et un coût de quelques centimes au plus. Les jours sans aucun nouveau document, **aucun
appel au LLM n'est fait**.

---

## 3. Les cinq idées qui structurent tout le projet

### 3.1 Une « machine à agents »

Un **agent** = une action surveillée = un dossier `agents/<id>/` avec deux fichiers :

- `config.yaml` : la position, les sources à lire, les règles ;
- `prompt.md` : le contexte métier rédigé en français pour le LLM (la thèse d'investissement, les nuances).

Ajouter une société (par exemple Take-Two) = créer un nouveau dossier, **sans toucher au code**, tant que les types
de sources dont elle a besoin existent déjà.

### 3.2 Le LLM identifie, le code décide

Le LLM a le droit de dire : « le document D3 correspond à la règle N-S4, voici la phrase clé, le nombre d'actions
nouvelles écrit dans le texte est 3 000 000, je suis sûr à 0,9 ». C'est tout.

Il n'a **pas** le droit de dire « vends ». Il ne connaît même pas l'action associée à la règle. C'est le code qui
calcule la dilution (3 000 000 / (50 941 528 + 3 000 000) = 5,6 %), la compare au seuil de 20 %, et en déduit
« rien à faire, information pour le suivi ».

### 3.3 Chaque agent est isolé

Si la config de NANO contient une erreur, ou si l'API de Claude plante pendant l'analyse de NANO, **UBI est traité
normalement**. Et l'état de NANO n'est pas modifié : ses documents ne sont pas marqués « vus », ils seront
retraités le lendemain. Rien n'est perdu.

### 3.4 La boîte d'envoi (outbox)

Une alerte n'est pas envoyée directement : elle est d'abord écrite en base avec la mention « pas encore envoyée ».
Le mail part ensuite ; ce n'est qu'après un envoi réussi que l'alerte est marquée « envoyée ». Si Gmail est en
panne ce jour-là, l'alerte repart automatiquement au run suivant. Aucune alerte ne peut se perdre.

### 3.5 Prudence par défaut

Dans le doute, le système répond **`RECO_UNCLEAR`** (« situation ambiguë : pas de recommandation, lis la source toi-même »)
plutôt que de recommander une vente ou un achat. Une recommandation dite **actionnable** (vendre tout, vendre la
moitié, acheter) exige :

- au moins une **source primaire** (un communiqué officiel : AMF, SEC, registre des essais), jamais seulement la presse ;
- une **confiance du LLM ≥ 0,8**.

Sinon, elle est rétrogradée en `RECO_UNCLEAR` et le mail explique pourquoi.

---

## 4. Visite guidée du dépôt, dossier par dossier

```
ai-agent-market-watcher/
├── agents/            ← CE QUE TU ÉDITES : les agents, leurs règles, leur contexte
├── watcher/           ← le code Python (le programme lui-même)
├── fixtures/          ← documents d'exemple étiquetés, pour tester le LLM
├── tests/             ← tests automatiques
├── deploy/            ← scripts pour le serveur
├── docs/              ← documentation
├── data/              ← (créé à l'exécution, jamais dans git) base de données, logs
├── Dockerfile, compose.yaml, .dockerignore   ← la « boîte » dans laquelle tourne le programme
├── requirements.txt, requirements-dev.txt    ← les bibliothèques Python utilisées
├── pyproject.toml     ← réglages des tests
├── .env.example       ← modèle du fichier des secrets
└── CLAUDE.md          ← consignes pour Claude Code (l'assistant qui a écrit le code)
```

### 4.1 `agents/` : la configuration métier

| Fichier | Rôle |
|---|---|
| `agents/_defaults.yaml` | Paramètres communs à tous les agents : modèles LLM utilisés, fenêtres de temps, seuils des garde-fous (confiance 0,8), seuil de l'anomalie de cours (20 %), budget de tokens par run (300 000), tarifs des modèles pour estimer le coût. |
| `agents/ubi/config.yaml` | Agent Ubisoft : position (prix d'entrée 5,33 €), sources (Google News FR/EN, AMF), mots-clés, règles `U-…`. |
| `agents/ubi/prompt.md` | Contexte rédigé pour le LLM : thèse (refinancement de la dette 2027), chiffres clés datés, nuances (« un report de jeu avec révision de guidance relève de U-S3 »). |
| `agents/nano/config.yaml` | Agent Nanobiotix : position, sources (ClinicalTrials.gov, SEC EDGAR, AMF, Google News), règles `N-…`. |
| `agents/nano/prompt.md` | Contexte de l'étude NANORAY-312, nuances (« en cas de doute entre N-B1 et N-S5, choisis N-S5 », « les publications mensuelles des droits de vote sont du bruit »). |

Ces fichiers sont **montés** dans le conteneur : les modifier ne demande pas de reconstruire le programme.

### 4.2 `watcher/` : le code

Le code est découpé en briques qui suivent l'ordre du run.

**Le chef d'orchestre**

| Fichier | Rôle |
|---|---|
| `run.py` | Point d'entrée. Lit la ligne de commande (`--all`, `--agent`, `--baseline`, `--inject`, `--test-mail`, `--heartbeat`), enchaîne les étapes pour chaque agent, gère les erreurs, enregistre le run, envoie les mails, prévient Healthchecks. |
| `settings.py` | Lit les **variables d'environnement** (le fichier `.env`) : clé API, identifiants Gmail, URLs Healthchecks, environnement `prod` ou `test`. Les secrets sont masqués dans les logs. Détermine où sont la base et les logs (`data/prod/` ou `data/test/`). |
| `config.py` | Décrit précisément ce qu'un `config.yaml` a le droit de contenir, et le vérifie. Une faute de frappe dans une clé est une erreur, pas un oubli silencieux. Charge chaque agent séparément, avec son `prompt.md`. |
| `models.py` | Les « formulaires » échangés entre les étapes : `NewsItem` (un document), `PriceSnapshot` (le cours), `TriageResult` et `RuleMatch` (ce que le LLM a le droit de produire), `Alert` (ce que le code produit). |

**Les entrées : cours et documents**

| Fichier | Rôle |
|---|---|
| `prices.py` | Cours de clôture. Source principale : Yahoo Finance (`yfinance`). Repli automatique : fichier CSV d'Euronext si Yahoo échoue ou s'il manque une séance. Écarte la séance du jour (pas encore clôturée). Détermine s'il y a une **nouvelle** clôture (sinon, week-end ou jour férié : règles de cours sautées). |
| `ingest.py` | Pour un agent : récupère le cours, interroge chaque source active, écarte les documents trop vieux, en double ou déjà vus. Une source en panne est notée comme avertissement, sans faire échouer l'agent. |
| `sources/base.py` | Le « contrat » que doit respecter tout type de source (valider ses paramètres, lister les documents, récupérer le texte complet) et le **registre** qui associe un type (`edgar`, `dila_amf`…) à son code. |
| `sources/http.py` | Client internet commun : délais maximum, 2 nouvelles tentatives en cas d'erreur réseau, identification explicite. |
| `sources/google_news.py` | Flux de recherche Google News (presse, **jamais** primaire). Gère la limitation de débit de Google. |
| `sources/dila_amf.py` | Informations réglementées déposées à l'AMF (communiqués officiels des sociétés françaises), via l'API info-financiere.gouv.fr, filtrées par ISIN. Texte extrait des PDF. |
| `sources/edgar.py` | Dépôts auprès de la SEC américaine (formulaires 6-K de Nanobiotix) : récupère le communiqué joint (exhibit 99). |
| `sources/clinicaltrials.py` | Registre ClinicalTrials.gov : garde une photo des champs clés de l'étude et produit un document « diff » dès qu'un champ change (statut, dates, résultats). |
| `sources/rss.py` | Flux RSS générique, pour une future société qui en publierait un. |
| `sources/text.py` | Extraction de texte brut depuis du HTML ou du PDF, troncature. |

**La couche LLM** (détaillée en section 6)

| Fichier | Rôle |
|---|---|
| `llm/__init__.py` | Enchaîne tri → récupération du texte complet des seuls documents retenus → analyse. Choisit les modèles au moment du run. |
| `llm/runtime.py` | Outils communs : création du modèle à partir de son nom, **budget de tokens** partagé par tout le run, références courtes `D1`, `D2`… |
| `llm/instructions.py` | **Tous les textes envoyés au LLM** : consignes globales, consignes du tri, mise en forme des règles, du cours et des documents. |
| `llm/triage.py` | Agent de tri (Haiku) et sa vérification de sortie. |
| `llm/analysis.py` | Agent d'analyse (Sonnet) et sa vérification de sortie. |
| `llm/evals.py` | Notation des evals : compare ce que le LLM a trouvé à ce qui était attendu. |
| `fixtures.py` | Lecture des documents d'exemple de `fixtures/` (en-tête + texte). |

**Le moteur de décision** (détaillé en section 7)

| Fichier | Rôle |
|---|---|
| `engine/metrics.py` | Tous les calculs : ratio cours / prix d'entrée, dilution, prime d'une offre, variation du jour. Si un calcul est impossible (chiffre manquant), il le **dit** au lieu d'inventer une valeur. |
| `engine/resolve.py` | Transforme un `RuleMatch` du LLM en `Alert` : règle encore active ? événement récent ? preuves primaires ? conditions (overrides) ? garde-fous ? rumeur ? surveillance à armer ? contradictions ? |
| `engine/price_rules.py` | Règles sur le cours (« cours ≥ 2 × prix d'entrée »), surveillances armées, anomalie de cours (± 20 % sans actualité). |
| `engine/time_rules.py` | Règles de date (« aucun résultat au 31/12/2028 », « détention de plus d'un an »). |
| `engine/priority.py` | Ordre des alertes : gravité, puis action (vendre tout > vendre la moitié > ambigu > …). |
| `engine/dedup.py` | Évite de t'envoyer deux fois la même alerte (fenêtre de 30 jours, sauf aggravation). |
| `engine/describe.py` | Textes lisibles des règles et des conditions pour les mails. |
| `engine/pipeline.py` | Enchaîne tout le moteur pour un agent et écrit les alertes dans la boîte d'envoi. |

**La mémoire, les mails, la surveillance**

| Fichier | Rôle |
|---|---|
| `store.py` | Base de données SQLite : création des tables, migrations, lectures et écritures, transaction par agent. |
| `notify/mailer.py` | Envoi SMTP via Gmail. Ajoute `[TEST]` devant l'objet en environnement de test. |
| `notify/templates.py` | Contenu des mails : alerte, digest, heartbeat, erreur de config, mail de test. |
| `notify/dispatch.py` | Vide la boîte d'envoi : regroupe les alertes par agent, envoie, marque « envoyée ». |
| `notify/heartbeat.py` | Rassemble les chiffres de la semaine pour le heartbeat. |
| `monitoring.py` | Fichier de logs (un par jour, gardé 30 jours) et signaux Healthchecks. |

### 4.3 `fixtures/` : les cas d'école du LLM

22 documents (11 par agent) au format « en-tête + texte ». L'en-tête dit ce qu'on attend :

```markdown
---
agent: NANO
source_name: SEC EDGAR
source_primary: true
synthetic: true                 # document inventé pour un événement qui n'a pas encore eu lieu
expected_rule_ids: [N-B1]       # la règle que le LLM doit reconnaître
---
NANOBIOTIX Announces That the Phase 3 NANORAY-312 Study ... Met Its Primary Endpoint ...
```

Deux sont de vrais documents (point semestriel et publication des droits de vote de Nanobiotix, qui ne doivent
**rien** déclencher), les autres sont synthétiques (OPA, échec de l'étude, levée de fonds…). Ils servent aux
**evals** (section 6.9) et à l'**injection** de bout en bout (`--inject`).

### 4.4 `tests/` : les tests automatiques

Un fichier de test par brique (`test_engine_resolve.py`, `test_sources_edgar.py`, `test_llm.py`…). Ils tournent
sans internet ni LLM, sur des réponses enregistrées (`tests/data/`). Le sous-dossier `tests/evals/` contient les
evals, qui, elles, appellent le vrai LLM et ne tournent que sur demande.

### 4.5 `deploy/` : les scripts du serveur

| Fichier | Rôle |
|---|---|
| `deploy/setup-vps.sh` | Prépare un serveur Ubuntu neuf en une commande : fuseau horaire, mises à jour de sécurité automatiques, Docker, pare-feu, SSH par clé uniquement, rotation du journal du cron. Peut être relancé sans risque. |
| `deploy/cron-run.sh` | Ce que le cron exécute chaque matin : lance le conteneur et ajoute sa sortie à `data/cron.log`. |

### 4.6 Les fichiers à la racine

| Fichier | Rôle |
|---|---|
| `Dockerfile` | Recette de l'image : Python 3.12, les bibliothèques, le code, un utilisateur non administrateur. |
| `compose.yaml` | Comment lancer l'image : lire `.env`, fuseau Paris, brancher les dossiers `data/` (écriture), `agents/` et `fixtures/` (lecture seule). Aucun port ouvert. |
| `.dockerignore` | Ce qui ne doit **jamais** entrer dans l'image (notamment `.env` et `data/`). |
| `requirements.txt` | Bibliothèques Python, versions figées : `pydantic-ai-slim[anthropic]` (agents LLM), `pydantic` (validation), `httpx` (internet), `feedparser` (RSS), `PyYAML`, `yfinance`, `pypdf`, `beautifulsoup4`… |
| `requirements-dev.txt` | En plus, pour développer : `pytest`, `pytest-cov`, `python-dotenv`. |
| `.env.example` | Modèle du fichier des secrets. Le vrai `.env` n'est jamais versionné. |

### 4.7 `data/` : ce que le programme écrit

Créé à l'exécution, jamais dans git.

```
data/
├── prod/watcher.sqlite      ← base de production
├── prod/logs/watcher.log    ← logs de production (un fichier par jour, 30 jours)
├── test/…                   ← même chose pour l'environnement de test
├── evals/evals-*.md         ← rapports des evals (en local)
└── cron.log                 ← sortie brute du cron (serveur)
```

---

## 5. Anatomie d'un agent : `config.yaml` et `prompt.md`

### 5.1 La position

```yaml
position:
  ticker: NANO
  isin: FR0011341205
  price_symbol: NANO.PA        # symbole chez Yahoo Finance
  status: OWNED                # WATCH (je surveille pour acheter) | OWNED (je détiens) | CLOSED (terminé)
  entry_price: 22.55           # obligatoire si OWNED
  entry_date: 2026-09-25
  shares_outstanding: 50941528 # nombre d'actions existantes (sert au calcul de dilution)
```

Le statut détermine quelles règles sont actives : une règle `phase: WATCH` ne s'applique que si tu ne détiens pas
encore la ligne, une règle `phase: OWNED` seulement si tu la détiens, `ANY` dans les deux cas. `CLOSED` éteint
l'agent.

### 5.2 Les sources

```yaml
sources:
  - name: SEC EDGAR
    type: edgar            # quel code utiliser
    primary: true          # communiqué officiel ? C'EST LA CONFIG QUI LE DIT, JAMAIS LE LLM
    params: {cik: "1760854", forms: ["6-K", "20-F"]}
  - name: Nanobiotix IR
    type: rss
    primary: true
    enabled: false         # désactivée (pas de flux exploitable, voir sources.md)
```

### 5.3 Les règles : trois familles

| `kind` | Qui l'évalue | Exemple |
|---|---|---|
| `event` | Le **LLM** reconnaît l'événement dans un document, le **code** décide | « L'étude n'a pas atteint son critère principal » |
| `price` | Le **code** seul, sur le cours | « Cours ≥ 2 × prix d'entrée » |
| `time` | Le **code** seul, sur la date | « Aucun résultat au 31/12/2028 » |

Champs communs :

| Champ | Sens |
|---|---|
| `id` | Identifiant affiché dans le mail. Convention : `N-B1` = Nanobiotix, **B**ullish (haussier), n° 1 ; `S` = baissier, `N` = neutre, `E` = entrée, `T` = temps. |
| `phase` | `WATCH`, `OWNED` ou `ANY` (voir 5.1). |
| `direction` | `bullish`, `bearish`, `neutral`, `entry`. Sert à détecter les contradictions. |
| `action` | Recommandation par défaut : `RECO_SELL_ALL`, `RECO_SELL_HALF`, `RECO_HOLD`, `RECO_UNCLEAR`, `RECO_BUY`, `RECO_NO_ENTRY`, `IGNORE`. |
| `severity` | `CRITICAL` ou `HIGH` → mail d'alerte immédiat ; `INFO` → digest. |
| `unless_fired` | Règle éteinte si l'une des règles listées s'est déjà déclenchée un jour (ex. N-S4 « levée de fonds » ne compte plus une fois les résultats connus). |

Champs propres aux règles `event` :

| Champ | Sens |
|---|---|
| `trigger` | La description de l'événement, **en français, montrée au LLM**. C'est le texte le plus important de la règle. |
| `figures` | Les chiffres à extraire, avec leur description (ex. `offer_price : Prix proposé en EUR par action…`). |
| `overrides` | Conditions calculées par le code qui changent l'action. Le premier override vrai l'emporte. |
| `arms` (dans un override) | Une **surveillance armée** : une condition de cours qui sera vérifiée chaque jour suivant. |
| `if_unavailable` (dans un override) | Si le chiffre manque : `unclear` (défaut) → `RECO_UNCLEAR` ; `skip` → on ignore cet override. |

Champs propres aux règles `price` : `when` (la condition) et `unless_armed` (règle éteinte tant qu'une surveillance
armée est active). Champs propres aux règles `time` : `deadline` (date butoir) **ou** `max_holding_days`
(durée de détention maximale).

Exemple complet commenté, la règle d'OPA sur Ubisoft :

```yaml
- id: U-B1
  kind: event
  phase: OWNED
  direction: bullish
  trigger: "Offre publique (OPA, OPR, OPAS) déposée ou annoncée sur les titres Ubisoft."
  figures:
    offer_price: >
      Prix de l'offre en EUR par action Ubisoft. Ne pas renseigner si le prix
      n'est pas exprimé en EUR par action.
  action: RECO_UNCLEAR                 # par défaut : ambigu (ex. prix non trouvé)
  severity: CRITICAL
  overrides:
    - id: U-B1                         # offre ≥ dernier cours : garder, et surveiller
      when: {metric: figure_vs_prev_close, figure: offer_price, op: ">=", value: 1.0}
      action: RECO_HOLD
      severity: CRITICAL
      arms:
        id: U-B1-EXIT                  # dès que le cours atteint 98 % du prix d'offre : tout vendre
        when: {metric: price_vs_figure, figure: offer_price, op: ">=", value: 0.98}
        action: RECO_SELL_ALL
        severity: CRITICAL
    - id: U-B2                         # offre à décote : ambigu
      when: {metric: figure_vs_prev_close, figure: offer_price, op: "<", value: 1.0}
      action: RECO_UNCLEAR
      severity: CRITICAL
```

La section 7.2 déroule ce que le système fait de cette règle, pas à pas.

### 5.4 Le `prompt.md`

C'est ton « briefing » à l'analyste. Il contient ce qu'un humain compétent devrait savoir pour bien lire les
documents : la thèse, les chiffres datés, et surtout les **nuances d'interprétation** qui départagent les cas
limites. Il ne contient **pas** les règles : elles sont injectées automatiquement depuis `config.yaml`, pour qu'il
n'existe qu'une seule source de vérité.

La date `context_updated_at` de la config dit quand ce contexte a été relu. Au-delà de 90 jours, le heartbeat te le
rappelle.

---

## 6. La couche LLM en profondeur

Cette section est la plus importante si tu veux réutiliser ces techniques. Elle part des notions de base et va
jusqu'aux détails d'implémentation.

### 6.1 Les notions de base

- **LLM** (Large Language Model) : un modèle qui reçoit du texte et produit du texte. Ici, les modèles Claude
  d'Anthropic, appelés via leur **API** (un service web payant à l'usage, distinct de l'abonnement Claude).
- **Token** : l'unité de facturation, environ ¾ de mot. On paie les tokens **en entrée** (ce qu'on envoie :
  consignes + documents) et, plus cher, les tokens **en sortie** (ce que le modèle écrit).
- **Instructions** (aussi appelées *system prompt*) : le texte qui dit au modèle qui il est et comment se comporter.
  Stable d'un jour à l'autre pour un agent donné.
- **Message** (ou *user prompt*) : ce qu'on lui soumet aujourd'hui : les documents du jour, le cours du jour.
- **Sortie structurée** : au lieu de laisser le modèle répondre en texte libre, on lui impose de remplir un
  formulaire précis (des champs, des types, des bornes). C'est ce qui permet au code d'exploiter la réponse sans
  ambiguïté.
- **PydanticAI** : la bibliothèque Python utilisée pour parler aux LLM. Elle gère les formulaires de sortie, les
  vérifications, les nouvelles tentatives et le comptage des tokens.

### 6.2 Pourquoi deux modèles : l'entonnoir

| | Tri | Analyse |
|---|---|---|
| Modèle | Claude **Haiku 4.5** (petit, rapide, bon marché) | Claude **Sonnet 5** (plus fort, plus cher) |
| Ce qu'il voit | Titre, source, date, résumé tronqué à 500 caractères | Texte complet (jusqu'à 15 000 caractères par document) |
| Question | « Lequel de ces documents pourrait concerner un événement surveillé ? » | « Lequel correspond exactement à quelle règle ? Preuves, chiffres, date, confiance » |
| Objectif | **Rappel** : ne rien laisser passer, quitte à garder trop | **Précision** : ne signaler que ce qui correspond vraiment |
| Lots | 50 documents par appel | 10 documents par appel |

Un matin typique : 40 articles Google News sur Ubisoft, dont 35 critiques de jeux. Le tri en garde 3. Seuls ces 3
voient leur **texte complet téléchargé** puis lu par Sonnet. On économise à la fois de l'argent (Sonnet lit 3
documents au lieu de 40) et des requêtes vers les sites.

Le tri est volontairement orienté **rappel**, et la consigne l'explique au modèle avec la raison :

> « En cas de doute, garde le document : un document écarté à tort ne sera jamais relu, alors qu'un document gardé
> à tort ne coûte qu'une lecture. »

Donner la **raison** d'une consigne, et pas seulement la consigne, aide le modèle à l'appliquer correctement dans
les cas imprévus.

### 6.3 Ce que le LLM reçoit réellement

Tout est assemblé dans `watcher/llm/instructions.py`. Pour l'analyse, les **instructions** sont la concaténation de
quatre blocs :

1. **Consignes globales** (communes à tous les agents, fixées par le cadrage §7.3) :
   « Tu es un agent de veille boursière 100 % consultatif… Ne décide jamais d'une action ni d'une sévérité… Ne fais
   aucun calcul… Si un chiffre demandé n'apparaît pas explicitement dans le document, omets la clé. N'estime jamais
   un chiffre absent… `confidence` mesure ta certitude que l'événement correspond exactement au déclencheur, pas la
   probabilité que l'information soit vraie… »
2. **Note sur les références** : les documents s'appellent `D1`, `D2`…, et seuls les noms de chiffres listés sont
   autorisés.
3. **Le `prompt.md` de l'agent** (thèse, contexte, nuances).
4. **Les règles `event` actives**, mises en forme automatiquement :

```text
### N-S4 (baissier)
Déclencheur : Levée de fonds ou émission d'actions / d'instruments donnant accès au capital de Nanobiotix.
Chiffres à extraire (omets la clé si le chiffre n'est pas écrit dans le document) :
- new_shares : Nombre maximal d'actions nouvelles pouvant être créées (actions émises + actions sous-jacentes aux instruments).
```

Le **message** du jour contient :

```text
Date du jour : 2026-09-27.

# Contexte de cours
Ligne suivie : NANO (Euronext Paris, EUR), position OWNED.
Dernière clôture : 22.9 EUR le 2026-09-26 (variation J-1 : +1.55 %).
Clôtures des dernières séances : 2026-09-15 : 21.8 ; … ; 2026-09-26 : 22.9

# Documents (2)

## [D1] Nanobiotix announces pricing of a €30 million offering
Source : SEC EDGAR ; date de publication : 2026-09-26

<texte du communiqué, tronqué à 15 000 caractères>

## [D2] …
```

Ce que le LLM **ne voit jamais**, volontairement :

- l'action et la gravité de chaque règle (`RECO_SELL_ALL`, `CRITICAL`…) : il ne peut donc pas être influencé par
  l'enjeu, ni « décider » ;
- les règles inactives (mauvaise phase, ou éteintes par `unless_fired`) : impossible de les déclencher ;
- les règles `price` et `time` : elles ne le concernent pas ;
- le caractère primaire ou non des sources : c'est la config qui le fixe.

Un test automatique (`test_analysis_instructions_contain_global_prompt_and_active_rules_only`) vérifie que les mots
`RECO_` et `CRITICAL` n'apparaissent jamais dans les consignes.

### 6.4 La sortie structurée : un formulaire, pas un texte libre

Le LLM d'analyse doit remplir ce formulaire (défini dans `watcher/models.py`) :

```python
class RuleMatch(BaseModel):
    rule_id: str                                # ex. "N-S4"
    item_ids: list[str] = Field(min_length=1)   # au moins une preuve : ["D1"]
    headline: str = Field(max_length=200)       # titre court
    rationale: str = Field(max_length=800)      # explication, 3 phrases, en citant le passage clé
    confidence: float = Field(ge=0, le=1)       # entre 0 et 1
    event_date: date                            # date de l'annonce, pas de l'article
    extracted_figures: dict[str, float] = {}    # ex. {"new_shares": 3000000}

class Analysis(BaseModel):
    matches: list[RuleMatch] = []               # liste vide = rien à signaler (le cas le plus fréquent)
```

**Comment ça marche techniquement** : PydanticAI transforme ce formulaire en un schéma JSON et le déclare au modèle
comme un **outil** (« tool ») à appeler pour rendre sa réponse. Le modèle « appelle l'outil » avec, en arguments, un
JSON qui respecte le schéma. PydanticAI vérifie alors ce JSON avec Pydantic : types, bornes (`confidence` entre 0 et
1), longueurs (`headline` ≤ 200 caractères), présence d'au moins une preuve. Exemple de sortie valide :

```json
{
  "matches": [{
    "rule_id": "N-S4",
    "item_ids": ["D1"],
    "headline": "Nanobiotix lève 30 M€ par émission de 3 millions d'actions nouvelles",
    "rationale": "Le communiqué annonce « the issuance of 3,000,000 new ordinary shares ». Il s'agit d'une émission d'actions, qui correspond au déclencheur de N-S4.",
    "confidence": 0.95,
    "event_date": "2026-09-26",
    "extracted_figures": {"new_shares": 3000000}
  }]
}
```

Tri : même principe avec un formulaire minimal, `TriageResult(relevant_ids: list[str])`.

Pourquoi c'est essentiel : le code en aval peut lire `match.extracted_figures["new_shares"]` sans jamais « parser »
une phrase. Si le modèle rend un formulaire invalide, ce n'est pas le code métier qui plante : c'est PydanticAI qui
le détecte et renvoie l'erreur au modèle (section suivante).

### 6.5 Vérifier et faire corriger : `output_validator` et `ModelRetry`

Deux niveaux de vérification s'enchaînent :

1. **Forme** (automatique) : le JSON respecte-t-il le schéma ? Sinon, PydanticAI renvoie au modèle le message
   d'erreur de validation et lui redemande.
2. **Fond** (écrit par nous) : un `output_validator` vérifie des règles métier que le schéma ne peut pas exprimer.

```python
@analyst.output_validator
def check_references(ctx: RunContext[AnalysisDeps], out: Analysis) -> Analysis:
    valid_rules = {r.id for r in ctx.deps.rules}
    for m in out.matches:
        if m.rule_id not in valid_rules:
            raise ModelRetry(f"rule_id inconnu : {m.rule_id}. IDs valides : {sorted(valid_rules)}")
        if unknown := unknown_refs(m.item_ids, ctx.deps.items):
            raise ModelRetry(f"item_ids inconnus : {unknown}. Références valides : {list(ctx.deps.items)}")
    return out
```

`ModelRetry` signifie : « renvoie ce message au modèle et redemande-lui ». Le modèle voit sa réponse précédente,
le message d'erreur (qui liste les valeurs valides), et corrige. L'agent est créé avec `retries=2` : jusqu'à
3 tentatives au total. Au-delà, l'appel échoue (section 6.10).

`ctx.deps` (les « dépendances ») est le contexte passé au moment de l'appel : les règles actives et les documents
fournis. C'est ce qui permet au validateur de savoir ce qui est valide **pour cet appel précis**.

#### Le point le plus subtil du projet : ne jamais exiger un chiffre

Il serait tentant d'ajouter au validateur : « la règle N-S4 demande `new_shares`, s'il manque, `ModelRetry` ». **C'est
exactement ce qu'il ne faut pas faire.** Si le chiffre n'est pas dans le document, forcer le modèle à réessayer le
pousse à en **inventer** un pour satisfaire la contrainte. Le validateur ne vérifie donc que ce qui est toujours
vérifiable (les IDs), jamais les chiffres.

Un chiffre manquant est traité **par le code** : le calcul lève `MetricUnavailable`, l'alerte devient
`RECO_UNCLEAR` avec la mention « chiffres non extraits, lis la source ». Tu es prévenu, sans qu'aucun chiffre ne soit
fabriqué.

Règle générale : **un retry doit porter sur une erreur que le modèle peut corriger honnêtement** (un ID mal recopié,
un format), jamais sur une information qu'il n'a pas.

Filet supplémentaire : si le modèle renvoie un chiffre que la règle ne demande pas, le code l'ignore et le note
dans les logs (`to_real_ids` dans `analysis.py`).

### 6.6 Les références courtes `D1`, `D2`…

Chaque document a un identifiant interne de 64 caractères (une empreinte sha256 de son URL). Demander au modèle de
les recopier provoquerait des erreurs de recopie, donc des retries, donc du coût. Le code présente les documents
sous les noms `D1`, `D2`…, le modèle répond avec ces noms, et le code les retraduit en identifiants réels après
validation (`assign_refs` dans `runtime.py`, `to_real_ids` dans `analysis.py`).

Principe réutilisable : **ne demande jamais au modèle de manipuler un identifiant long ou technique**. Donne-lui
des alias courts et traduis-les toi-même.

### 6.7 Les garde-fous autour du LLM

Le LLM peut se tromper. Le système est conçu pour qu'une erreur du LLM ne se transforme **jamais** directement en
recommandation de vente. Les protections, de la plus en amont à la plus en aval :

| Garde-fou | Où | Protège contre |
|---|---|---|
| Le LLM ne voit que les règles actives, sans action ni gravité | `instructions.py` | Déclencher une règle hors sujet, être influencé par l'enjeu |
| Consignes « omets la clé », « n'estime jamais », « aucun calcul » | `instructions.py` | Chiffres inventés, erreurs de calcul |
| Validation de forme + `output_validator` | `analysis.py`, `triage.py` | Règle ou document inexistant |
| Pas de retry sur un chiffre manquant | `analysis.py` | Chiffres inventés sous la contrainte |
| Tous les calculs dans le code | `engine/metrics.py` | Erreurs arithmétiques du LLM |
| `source_primary` vient de la config | `config.yaml`, `resolve.py` | Le LLM qui qualifierait un article de presse d'« officiel » |
| Action actionnable ⇒ source primaire **et** confiance ≥ 0,8, sinon `RECO_UNCLEAR` | `resolve.py` | Vendre sur une rumeur ou sur une lecture incertaine |
| Une rumeur n'est jamais `CRITICAL` | `resolve.py`, `config.py` | Panique sur un article de presse |
| Événement de plus de 7 jours ignoré | `resolve.py` | Un article récapitulatif qui relance un vieil événement |
| Deux règles opposées sur le même document ⇒ `RECO_UNCLEAR` | `resolve.py` | Lecture contradictoire |
| Preuves (liens), passage cité, confiance et raisons de rétrogradation affichés dans le mail | `templates.py` | Te permettre de vérifier toi-même en 30 secondes |
| Anomalie de cours (± 20 % sans actualité détectée) | `price_rules.py` | Un événement que le LLM aurait manqué (filet de sécurité) |

### 6.8 Coûts et plafonds

- **Plafond par réponse** (`max_tokens`) : 2 048 tokens pour le tri (une simple liste de références), 16 000 pour
  l'analyse. Sonnet 5 peut « réfléchir » avant de répondre et cette réflexion compte dans la limite, d'où la marge.
- **Plafond par appel** : 4 requêtes maximum (1 tentative + 2 retries + 1 de marge).
- **Plafond par run** : 300 000 tokens au total, partagés par tous les agents (`TokenBudget` dans `runtime.py`,
  réglable dans `_defaults.yaml` → `llm_budget`). Chaque appel reçoit comme limite le budget restant. S'il est
  épuisé, le run s'arrête proprement et passe en échec (`_budget` dans les erreurs). Cela protège d'une boucle ou
  d'un afflux anormal de documents.
- **Suivi** : les tokens sont comptés par modèle, même quand un appel échoue, enregistrés dans la table `runs`, et
  convertis en coût estimé dans le heartbeat grâce aux tarifs de `_defaults.yaml` → `llm_pricing`.
- **Ordre de grandeur** : quelques dollars par mois pour deux agents ; zéro les jours sans nouveau document.

### 6.9 Choisir le modèle au moment du run

Les agents PydanticAI (`triage_agent`, `analyst`) sont créés **sans modèle**. Le modèle leur est passé à chaque
appel (`run_sync(..., model=model)`). Il est lu dans cet ordre :

1. la variable d'environnement `WATCHER_MODEL_TRIAGE` / `WATCHER_MODEL_ANALYSIS` si elle est renseignée ;
2. sinon `_defaults.yaml` → `models`.

Conséquences : changer de modèle ne demande qu'une ligne de config ; les evals peuvent comparer plusieurs modèles
(y compris d'autres fournisseurs, au format `fournisseur:modèle`) sans toucher au code ; les tests remplacent le
modèle par un faux modèle scripté.

### 6.10 Quand le LLM échoue

Toute erreur de la couche LLM (API indisponible, clé invalide, retries épuisés, budget dépassé) devient une
`LlmError`. Conséquences, toutes voulues :

- l'agent concerné est en **échec pour ce run** ; les autres continuent ;
- son état n'est **pas enregistré** : ses documents ne sont pas marqués vus et seront retraités au run suivant ;
- le run est marqué `partial` ou `failed`, Healthchecks reçoit un signal d'échec avec le résumé des erreurs.

Une exception : si le **texte complet** d'un document retenu ne peut pas être téléchargé, l'analyse se fait sur le
titre et le résumé, avec un avertissement. Une source capricieuse ne bloque pas la veille.

### 6.11 Tester un système qui contient un LLM : trois niveaux

C'est la partie la plus transposable du projet.

**Niveau 1 : tests unitaires, sans LLM (gratuits, à chaque modification).**
Les tests de `tests/test_llm.py` remplacent Claude par un faux modèle (`ScriptedModel`, basé sur le `FunctionModel`
de PydanticAI) qui renvoie des réponses écrites à l'avance. On vérifie la **plomberie**, pas l'intelligence :

- les consignes contiennent bien le `prompt.md` et les règles actives, et jamais les actions ;
- si le faux modèle renvoie un `rule_id` inconnu, un retry est bien demandé avec le bon message, puis la 2ᵉ réponse
  est acceptée ;
- une liste vide est acceptée ;
- un chiffre manquant ne déclenche **jamais** de retry (`test_missing_figure_is_never_retried`) ;
- en cas d'échec de l'analyse, l'agent échoue et ses documents restent « non vus » (`tests/test_llm_run.py`) ;
- le budget de tokens est respecté ;
- les documents sont bien présentés en `D1`, `D2`… et tronqués.

**Niveau 2 : evals, avec le vrai LLM (quelques centimes, à la demande : `pytest -m eval`).**
Chaque fixture passe par le vrai tri puis la vraie analyse, et le résultat est noté par `llm/evals.py`. Les critères
sont **asymétriques**, parce que toutes les erreurs n'ont pas le même coût :

| Critère | Pourquoi |
|---|---|
| **Zéro faux négatif** sur les cas qui mènent à « vendre toute la ligne » avec source primaire | Rater un échec de l'étude clinique coûte le plus cher |
| **Zéro faux positif de vente** | Une vente injustifiée est l'autre erreur grave |
| **Chiffres exacts** là où ils sont attendus | Un seuil de dilution calculé sur un faux chiffre fausse la décision |
| Autres écarts (règle neutre manquée, règle en trop sans conséquence) | Rapportés, mais tolérés |

Le rapport est un tableau par cas et par modèle, écrit dans `data/evals/`. Résultat du 26/09/2026 avec Haiku +
Sonnet : **22/22 cas conformes, dont 20 sans aucun écart**. Les deux écarts sont instructifs :

- `NANO/endpoint_met` : le document annonce un critère principal atteint, mais avec une survie globale seulement
  « en tendance favorable ». Le modèle a choisi N-S5 (résultat mitigé → `RECO_UNCLEAR`) au lieu de N-B1. C'est
  l'effet de la consigne « en cas de doute entre N-B1 et N-S5, choisis N-S5 » : le comportement prudent voulu, sans
  conséquence de vente.
- `UBI/game_delay_no_guidance` : le tri (Haiku) écarte le report de jeu sans révision de guidance (U-N1, règle
  neutre). Limite connue : une règle neutre manquée ne produit qu'une information de suivi en moins.

Principe : **les evals mesurent le comportement du modèle sur des cas que tu as choisis, avec des critères qui
reflètent le coût réel des erreurs**. On les relance après toute modification d'un `prompt.md`, d'un `trigger`,
des consignes, ou avant de changer de modèle.

**Niveau 3 : injection de bout en bout (quelques centimes, un vrai mail `[TEST]`).**
`--inject fixtures/nano/endpoint_met.md --primary` fait passer un document par tout le pipeline (tri, analyse,
moteur, mail), dans l'environnement de test. C'est la vérification finale « en conditions réelles ».

### 6.12 Récapitulatif du flux LLM d'un agent

```
nouveaux documents (ex. 40)
   │  instructions du tri : société + thèse + mots-clés + déclencheurs (sans IDs)
   │  message : [D1] titre / source / date / résumé 500 car. … (lots de 50)
   ▼
Haiku → TriageResult {relevant_ids: ["D4","D17"]}
   │  validation : références existantes ? sinon ModelRetry
   │  traduction D4 → id réel
   ▼
texte complet téléchargé pour les 2 documents retenus seulement
   │  instructions de l'analyse : consignes globales + note D1… + prompt.md + règles actives
   │  message : date du jour + contexte de cours + documents complets (lots de 10)
   ▼
Sonnet → Analysis {matches: [RuleMatch…]}
   │  validation de forme (Pydantic) + output_validator (rule_id, item_ids) → ModelRetry si besoin
   │  traduction des références, chiffres non demandés ignorés
   ▼
RuleMatch[] → moteur de règles (code) → Alert[]
```

---

## 7. Le moteur de règles : là où se prennent les décisions

### 7.1 De la reconnaissance à l'alerte

Pour chaque `RuleMatch` rendu par le LLM, `engine/resolve.py` applique ces étapes, dans l'ordre :

1. **Règle active ?** Sinon, ignoré (défense en profondeur : le LLM ne la voyait déjà pas).
2. **Fraîcheur** : événement daté de plus de 7 jours (`max_event_age_days`) → ignoré.
3. **Preuves** : on reconstruit les liens à partir des documents cités. Si **aucune** preuve ne vient d'une source
   primaire, c'est une **rumeur**.
4. **Overrides** : on calcule les métriques et on teste les conditions dans l'ordre ; la première vraie l'emporte.
   Si un chiffre nécessaire manque, `RECO_UNCLEAR` / `HIGH` avec « chiffres non extraits, lis la source ».
5. **Garde-fou actionnable** : action de vente ou d'achat + (rumeur ou confiance < 0,8) → `RECO_UNCLEAR`, avec
   la raison.
6. **Rumeur** : gravité ramenée à `INFO`, remontée à `HIGH` si le cours a bougé d'au moins 10 % lors de la dernière
   séance. Jamais
   `CRITICAL`.
7. **Armement** : si l'override retenu prévoit une surveillance, et que le match n'est ni une rumeur ni rétrogradé,
   elle est créée en base avec sa valeur de référence (ex. le prix d'offre).
8. **Contradictions** : deux matches de sens opposé (haussier / baissier) sur le même document → tous deux
   `RECO_UNCLEAR`.

Puis `engine/pipeline.py` ajoute les règles calculées par le code seul :

- **règles `price`** : seulement si tu détiens la ligne et qu'il y a une nouvelle clôture ; une seule fois par
  position ;
- **surveillances armées** : vérifiées à chaque nouvelle clôture, y compris dans le run qui les crée ; se
  déclenchent une seule fois ; annulées si la position passe en `CLOSED` ;
- **règles `time`** : échéance atteinte → une alerte, une seule fois ;
- **anomalie de cours `P-ANOMALY`** : variation d'au moins 20 % en une séance sans aucun match ce jour-là ni
  alerte d'actualité depuis 3 jours → `HIGH` / `RECO_UNCLEAR` « mouvement inexpliqué, cherche la source ».

Enfin : tri par priorité, **dédoublonnage** (une alerte identique dans les 30 derniers jours est supprimée, sauf si
la nouvelle est plus grave : passage de rumeur à source primaire, gravité supérieure ou action plus prioritaire), et
écriture dans la boîte d'envoi. Les matches `IGNORE` ne sont jamais envoyés, seulement journalisés.

### 7.2 Exemple déroulé : une OPA sur Ubisoft

Hypothèse : l'AMF publie un communiqué « Offre publique d'achat à 7,00 € par action ». La veille de l'annonce,
Ubisoft clôturait à 5,50 €. Ton prix d'entrée est 5,33 €.

1. **Fetch** : la source AMF (primaire) remonte le communiqué.
2. **Tri** : Haiku voit le titre, reconnaît une offre publique, garde le document.
3. **Analyse** : Sonnet rend `rule_id: U-B1`, `item_ids: [D1]`, `offer_price: 7.0`, `confidence: 0.97`.
4. **Moteur** :
   - preuve primaire (AMF) → pas une rumeur ;
   - override 1 : `figure_vs_prev_close` = 7,00 / 5,50 = 1,273 ≥ 1,0 → vrai → `U-B1`, `RECO_HOLD`, `CRITICAL` ;
   - pas de rétrogradation (`RECO_HOLD` n'est pas actionnable) ;
   - armement de `U-B1-EXIT` avec la référence 7,00 : elle se déclenchera quand clôture / 7,00 ≥ 0,98, soit un
     cours ≥ 6,86 € ; comme cette surveillance mènera à « vendre toute la ligne », la confiance du match doit être
     ≥ 0,8 pour l'armer : c'est le cas ;
   - la surveillance est évaluée tout de suite : si la clôture du jour est déjà ≥ 6,86 €, une deuxième alerte
     `U-B1-EXIT` / `RECO_SELL_ALL` part dans le même mail.
5. **Mail** : `[CRITICAL] UBI · Information de suivi · …`, avec le calcul affiché (« 1,273, soit +27,3 % ; seuil :
   ≥ 1 → condition remplie »), le cours, l'écart au prix d'entrée, le lien AMF marqué `[source primaire]`, la
   confiance.

Variantes :

- **Même annonce, mais seulement dans la presse** (Google News) : rumeur → gravité `INFO` (ou `HIGH` si le cours a
  bondi d'au moins 10 %), **pas de surveillance armée**, mention `[RUMEUR — presse]`. Le lendemain, quand le
  communiqué AMF arrive, le dédoublonnage laisse passer l'alerte, car c'est une escalade (rumeur → primaire).
- **Prix exprimé uniquement en dollars** : la consigne dit « ne pas renseigner », `offer_price` est omis, les
  overrides ne sont pas calculables → `RECO_UNCLEAR` / `HIGH`, « chiffres non extraits, lis la source ».
- **Offre à 5,00 €** : 5,00 / 5,50 = 0,909 < 1 → override `U-B2`, `RECO_UNCLEAR` (offre à décote).

Tu vois ici la répartition des rôles : le LLM a lu un texte et recopié un nombre ; tout le reste (ratio, seuils,
surveillance, gravité, rumeur, dédoublonnage) est du code déterministe, testé, qui donne toujours la même réponse.

---

## 8. Sources de données et cours de bourse

| Source | Type | Primaire | Pour | Particularités |
|---|---|---|---|---|
| AMF (info-financiere.gouv.fr) | `dila_amf` | oui | UBI, NANO | Communiqués réglementés, filtrés par ISIN ; texte extrait du PDF ; versions FR et EN séparées. |
| SEC EDGAR | `edgar` | oui | NANO | Formulaires 6-K / 20-F ; exige `SEC_USER_AGENT` avec un contact (politique SEC). |
| ClinicalTrials.gov | `clinicaltrials` | oui | NANO | Pas de « documents » : le code compare une photo des champs clés d'un jour sur l'autre et fabrique un document « diff ». Le premier passage sert de référence. |
| Google News | `google_news_rss` | **non** | UBI, NANO | Presse ; le texte complet est récupéré en « meilleur effort ». Google limite le débit : le code espace et plafonne les requêtes. |

Toutes les sources partagent les mêmes règles : délais maximum, 2 nouvelles tentatives, et **une source en panne
n'arrête jamais un agent** (elle est signalée dans le heartbeat).

**Cours** : Yahoo Finance d'abord, CSV Euronext en repli. Sans cours, les règles de cours et l'anomalie sont
sautées ce jour-là, mais l'analyse des documents continue. Le détail des choix est dans `docs/sources.md`.

**Premier démarrage** (`--baseline`) : sans lui, le premier run analyserait tout l'historique des flux et
enverrait des alertes sur de vieux événements. Le baseline marque tout ce qui existe comme « vu » et prend la photo
de référence de ClinicalTrials, sans LLM ni mail.

---

## 9. La mémoire du système : la base SQLite

Une base SQLite est un simple fichier (`data/prod/watcher.sqlite`) qui contient des tables.

| Table | Contenu | Utilité |
|---|---|---|
| `seen_items` | Chaque document déjà traité, par agent (titre, URL, source, retenu ou non au tri) | Ne jamais retraiter ni repayer un document |
| `events` | Chaque alerte produite, avec sa date d'envoi (`sent_at`, vide tant qu'elle n'est pas partie) | Boîte d'envoi, dédoublonnage, `unless_fired`, historique |
| `armed_watches` | Surveillances armées (`active`, `fired`, `cancelled`) | Ex. « tout vendre si le cours atteint 98 % du prix d'offre » |
| `source_state` | Mémoire propre à chaque source : photo ClinicalTrials, dernière clôture traitée | Détecter les changements et les nouvelles clôtures |
| `runs` | Chaque exécution : portée, statut, durée, agents OK / en échec, alertes, tokens, erreurs | Heartbeat, diagnostic |
| `config_errors` | Erreurs de configuration détectées, notifiées, résolues | Un seul mail par erreur, rappel dans le heartbeat |
| `heartbeats` | Jours où le heartbeat est parti | Ne pas l'envoyer deux fois |
| `schema_version` | Version du schéma de la base | Migrations automatiques quand le code évolue |

Toutes les écritures d'un agent se font dans une **transaction** : soit tout est enregistré (l'agent a réussi),
soit rien (l'agent a échoué). Les horodatages sont en UTC.

Les environnements `prod` et `test` ont chacun leur base : un essai ne pollue jamais la production.

---

## 10. Mails et surveillance du bon fonctionnement

### 10.1 Les mails que tu peux recevoir

| Objet | Quand | Que faire |
|---|---|---|
| `[CRITICAL] NANO · Vendre toute la ligne · …` | Au moins une alerte `CRITICAL` ou `HIGH` pour un agent (un mail par agent, alertes triées) | Lire l'alerte de tête, ouvrir la preuve, décider toi-même |
| `[HIGH] …` | Idem, gravité moindre (souvent `RECO_UNCLEAR`) | Lire la source |
| `[INFO] Veille du JJ/MM — N éléments` | Au moins une alerte `INFO` dans le run (un seul digest pour tous les agents) | Information de suivi |
| `[HEARTBEAT] Semaine du JJ/MM — X/7 runs OK, Y alertes` | Chaque lundi après le run | Vérifier 7/7, les avertissements, le coût |
| `[CONFIG] UBI · agent désactivé : configuration invalide` | Première détection d'une erreur dans un `config.yaml` | Corriger la config (l'agent est à l'arrêt tant que ce n'est pas fait) |
| `[TEST] …` | Tout mail de l'environnement de test | Rien : c'est un essai |

Chaque mail d'alerte se termine par : « Recommandation automatique issue de tes règles. Aucune opération n'a été
exécutée. »

### 10.2 Healthchecks.io : savoir que le silence est normal

Le programme envoie « je démarre » puis « OK » ou « échec » (avec le résumé des erreurs) à une URL secrète de
Healthchecks. Healthchecks attend un signal chaque jour à 07:00, avec 2 heures de tolérance : **s'il ne reçoit rien**
(serveur éteint, cron cassé, clé expirée…), c'est lui qui t'envoie un mail. C'est la seule façon de distinguer
« rien à signaler » de « plus rien ne tourne ».

Seul le run quotidien `--all` signale à Healthchecks ; les runs manuels (`--agent`, `--inject`) ne le font pas.

### 10.3 Le heartbeat du lundi

Il résume la semaine : runs des 7 derniers jours (statut, durée), alertes envoyées par agent, statut de chaque agent,
sources en erreur, surveillances armées actives, tokens et coût estimé, et des **rappels** : contexte d'un
`prompt.md` de plus de 90 jours, `shares_outstanding` absent ou vieux de plus de 60 jours, sources désactivées,
échéances `time` à moins de 30 jours.

### 10.4 Les logs

`data/prod/logs/watcher.log` : une ligne par étape et par agent (documents récupérés, retenus au tri, matches,
alertes, tokens). C'est le premier endroit où regarder en cas de doute. Les commandes pour les lire sont dans
`exploitation.md`.

---

## 11. Tests : comment on sait que ça marche

| Type | Commande | Réseau / LLM | Quand |
|---|---|---|---|
| Tests unitaires (config, moteur, sources sur réponses enregistrées, plomberie LLM avec faux modèle, mails, base) | `pytest` | Non | À chaque modification du code |
| Couverture du moteur (objectif ≥ 90 %) | `pytest --cov=watcher/engine` | Non | Après une modification du moteur |
| Evals | `pytest -m eval` | Oui, payant | Après une modification d'un prompt, d'un `trigger`, des consignes, ou pour comparer des modèles |
| Injection de bout en bout | `python -m watcher.run --agent NANO --inject fixtures/nano/<cas>.md --primary` | Oui, payant, vrai mail `[TEST]` | Validation finale |

Deux principes des tests unitaires : les dates sont **injectées** (jamais « aujourd'hui » en dur, pour que les tests
donnent le même résultat tous les jours), et aucun test par défaut n'appelle internet ni le LLM.

---

## 12. Déploiement : conteneur, VPS, cron

- **Docker** met le programme et toutes ses bibliothèques dans une « boîte » (l'**image**) qui fonctionne à
  l'identique sur ton PC et sur le serveur. Un **conteneur** est une exécution de cette image.
- **Docker Compose** (`compose.yaml`) décrit comment lancer le conteneur : quel fichier de secrets lire, quels
  dossiers brancher.
- **Le VPS** est un petit serveur Ubuntu loué chez OVHcloud, sécurisé par `deploy/setup-vps.sh` (connexion par
  clé SSH uniquement, pare-feu, mises à jour automatiques).
- **Le cron** est le réveil du serveur : chaque jour à 07:00, il lance `deploy/cron-run.sh`, qui démarre un
  conteneur, exécute le run, puis supprime le conteneur. Rien ne tourne en permanence et aucun port n'est ouvert.
- **Les secrets** (clé API, mot de passe Gmail, URLs Healthchecks) sont dans `/opt/watcher/.env` sur le serveur,
  lisible par ton seul utilisateur, jamais dans git ni dans l'image.

La procédure complète est dans `docs/deploiement.md`, les commandes du quotidien dans `docs/exploitation.md`.

---

## 13. Ce que tu peux réutiliser dans d'autres projets LLM

Une liste de patterns, dans l'ordre où tu les rencontreras en concevant un projet.

1. **Sépare la perception de la décision.** Le LLM lit, reconnaît, extrait. Le code calcule, décide, agit. Toute
   décision qui a des conséquences doit être prise par du code testable et déterministe.
2. **Impose une sortie structurée.** Définis un formulaire Pydantic avec des types et des bornes. Rends
   possible et normale la réponse « rien » (liste vide) et dis au modèle que c'est la réponse la plus fréquente,
   sinon il aura tendance à trouver quelque chose.
3. **Valide le fond, pas seulement la forme**, avec un `output_validator`, et renvoie un message d'erreur qui liste
   les valeurs valides (`ModelRetry`) : le modèle se corrige presque toujours du premier coup.
4. **Ne fais jamais réessayer le modèle sur une information absente.** Rends le champ optionnel et gère l'absence
   dans le code. Sinon, tu fabriques des hallucinations.
5. **Donne des alias courts** (`D1`, `D2`) aux objets que le modèle doit citer, et retraduis-les toi-même.
6. **Montre au modèle le strict nécessaire.** Pas les enjeux, pas les règles inactives, pas les décisions. Moins
   il en voit, moins il peut être influencé ou se tromper de cible.
7. **Fais un entonnoir de modèles** : un petit modèle orienté rappel pour filtrer beaucoup de bruit, un modèle plus
   fort orienté précision sur ce qui reste. Ne télécharge le détail que pour ce qui passe le filtre.
8. **Explique le pourquoi des consignes** et donne des règles de départage explicites pour les cas limites (« en cas
   de doute entre X et Y, choisis Y »). Mets le savoir métier dans un fichier texte éditable (`prompt.md`), séparé
   du code.
9. **Sépare les consignes stables des messages du jour.** C'est plus lisible, plus testable, et c'est la structure
   qui permet ensuite d'optimiser les coûts (mise en cache des consignes côté fournisseur).
10. **Garde la provenance hors du LLM.** Ce qui qualifie une information (source officielle ou non) doit venir de ta
    config, pas de l'appréciation du modèle.
11. **Plafonne tout** : tokens par réponse, requêtes par appel, tokens par exécution. Compte les tokens même en cas
    d'échec, et suis le coût.
12. **Choisis le modèle au moment de l'appel**, pas à la construction : changer de modèle ou en comparer plusieurs
    devient une ligne de config.
13. **Teste à trois niveaux** : plomberie avec un faux modèle (gratuit, systématique), evals sur cas étiquetés
    avec le vrai modèle (payant, à la demande), bout en bout (validation finale).
14. **Écris des critères d'eval asymétriques** qui reflètent le coût réel de chaque type d'erreur, plutôt qu'un
    simple pourcentage de réussite.
15. **Rends les échecs rejouables** : si le LLM échoue, n'enregistre rien et laisse le prochain passage refaire le
    travail. Écris les résultats dans une boîte d'envoi avant de les envoyer.
16. **Rends chaque sortie vérifiable par un humain** : preuves (liens), passage cité, confiance, raisons de toute
    rétrogradation. Le système recommande ; l'humain décide en connaissance de cause.
17. **Surveille le silence** : un système qui « ne dit rien » doit prouver qu'il tourne (Healthchecks, heartbeat).

---

## 14. Glossaire

| Terme | Sens dans ce projet |
|---|---|
| **Agent** | Une action surveillée : un dossier `agents/<id>/`. Par extension, les deux « agents LLM » (tri et analyse) qui travaillent pour elle. |
| **API** | Service web qu'un programme appelle (API Anthropic pour Claude, API de la SEC, de l'AMF…). |
| **Armer / surveillance armée** | Créer une condition de cours à vérifier chaque jour suivant un événement (ex. après une OPA). |
| **Baseline** | Premier passage qui marque tout comme vu, sans LLM ni mail. |
| **Cron** | Planificateur de tâches du serveur Linux. |
| **Dédoublonnage** | Ne pas renvoyer une alerte déjà envoyée récemment. |
| **Digest** | Mail récapitulatif des alertes `INFO`. |
| **Eval** | Test du comportement réel du LLM sur des cas étiquetés, noté selon des critères d'acceptation. |
| **Fixture** | Document d'exemple étiqueté (`fixtures/`). |
| **Heartbeat** | Mail hebdomadaire qui prouve que tout tourne et résume la semaine. |
| **Healthchecks** | Service externe qui t'alerte si le programme ne donne pas signe de vie. |
| **ISIN** | Identifiant international d'une action (ex. FR0011341205 pour Nanobiotix). |
| **LLM** | Grand modèle de langage (ici Claude Haiku et Claude Sonnet). |
| **Match** (`RuleMatch`) | Ce que rend l'analyse : « tel document correspond à telle règle ». |
| **Outbox** | Boîte d'envoi en base : alertes écrites avant d'être envoyées, renvoyées si l'envoi échoue. |
| **Override** | Condition calculée qui remplace l'action par défaut d'une règle. |
| **Primaire (source)** | Source officielle (communiqué de la société, régulateur, registre). Seule capable de justifier une vente ou un achat. |
| **PydanticAI** | Bibliothèque Python pour construire des agents LLM avec sortie structurée et validation. |
| **Rappel / précision** | Rappel : ne rien manquer. Précision : ne rien signaler à tort. |
| **Retry / `ModelRetry`** | Nouvelle tentative demandée au modèle, avec un message expliquant l'erreur. |
| **Rumeur** | Match dont aucune preuve ne vient d'une source primaire. |
| **Run** | Une exécution du programme. |
| **Token** | Unité de texte facturée par le fournisseur du LLM (≈ ¾ de mot). |
| **Transaction** | Groupe d'écritures en base enregistré en entier ou pas du tout. |
| **VPS** | Serveur virtuel loué (OVHcloud). |
