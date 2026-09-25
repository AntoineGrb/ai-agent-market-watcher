# Cadrage — Watcher : machine à agents de veille boursière

> Version 1.0 — 25/09/2026 — document destiné à Claude Code.
> Remplace `watch_models.py` et `watch_rules.yaml` (v1.2). Le fichier `watch_agents_spec.md` (v1.2) reste la source du **contenu rédactionnel** des prompts agents (voir §7.4), mais ses tableaux de règles et sa section 0 sont remplacés par ce document.

---

## 0. Mode d'emploi pour Claude Code

- Lis tout le document avant d'écrire du code. Les décisions de la §2 sont **actées** : ne les rediscute pas, signale uniquement une impossibilité technique.
- Les éléments marqués 🔲 sont à compléter par l'utilisateur ou à investiguer à l'étape 0. **Ne les invente pas** : laisse la valeur à `null` ou la source en `enabled: false`.
- Implémente étape par étape (§13). Chaque étape a un critère de fin ; ne passe pas à la suivante sans tests verts.
- Utilise uniquement les API stables et actuelles des librairies, en particulier PydanticAI. Épingle les versions dans `requirements.txt`. En cas de doute sur une signature, vérifie la doc de la version installée plutôt que de supposer.
- Code production-ready : typage complet, gestion d'erreurs explicite, logs exploitables, aucune clé en clair.

---

## 1. Contexte, objectif, périmètre

### 1.1 Contexte

Projet personnel d'apprentissage des patterns agentiques (structured output, garde-fous, validation avec retry, evals, orchestration d'un pipeline LLM + code) avec une vraie utilité pour l'utilisateur.

### 1.2 Objectif

Un job quotidien qui surveille des lignes boursières (actualités + cours) et envoie par email une **recommandation** quand un indicateur défini par l'utilisateur est atteint. **Silence** s'il n'y a rien de pertinent. Le bon fonctionnement est prouvé par un monitoring externe (Healthchecks), pas par l'arrivée de mails.

**Machine à agents** : ajouter une société = ajouter un dossier `agents/<id>/` (config + prompt), sans modifier le code, tant que les types de sources nécessaires existent déjà.

### 1.3 Agents initiaux

| Agent | Société | Ligne | ISIN | Statut initial |
|---|---|---|---|---|
| `UBI` | Ubisoft Entertainment | UBI, Euronext Paris, EUR | FR0000054470 | `OWNED` |
| `NANO` | Nanobiotix | NANO, Euronext Paris, EUR | FR0011341205 | `OWNED` |

### 1.4 Hors périmètre (non négociable)

- **Aucune exécution d'ordre**, aucun accès au courtier (Trade Republic) ni au portefeuille. Agents 100 % consultatifs.
- Statut de position, prix et date d'entrée sont **saisis à la main** dans la config.
- Les recommandations appliquent les règles écrites par l'utilisateur. Le système ne produit aucune recommandation qui ne découle pas d'une règle de la config.

---

## 2. Décisions actées

| Sujet | Décision |
|---|---|
| Framework agent | PydanticAI, Python 3.12 |
| Répartition LLM / code | Le LLM **identifie des événements** (`rule_id`) et **extrait des chiffres bruts**, avec preuves sous forme d'IDs de documents. Le **code** décide de l'action et de la sévérité, et calcule tous les seuils, dilutions, primes, dates. |
| Types de règles | `event` (détectée par le LLM), `price` et `time` (évaluées par le code). Les règles `event` peuvent avoir des `overrides` (action conditionnée à une métrique calculée) et des surveillances armées (`arms`). |
| Modèles | Tri : Claude Haiku 4.5. Analyse : Claude Sonnet 5. Configurables dans `_defaults.yaml`. |
| Planification | 1 run par jour à 07:00 Europe/Paris, 7 jours sur 7. Règles de prix évaluées sur la dernière clôture, et sautées s'il n'y a pas de nouvelle clôture (week-end, jour férié). |
| Hébergement | VPS OVHcloud, Docker Compose, cron sur l'hôte. VPS mutualisable avec d'autres projets. |
| Persistance | SQLite dans un volume monté. |
| Email | Gmail SMTP + mot de passe d'application, via `smtplib`. |
| Monitoring | Healthchecks.io (ping succès / échec) + log fichier + mail heartbeat hebdomadaire. |
| Rumeurs | Match dont **toutes** les preuves viennent de sources `primary: false`. Sévérité INFO par défaut, promue HIGH si \|variation J-1\| ≥ 10 %. Jamais d'action « actionnable » (vente ou achat). |
| Anomalie de prix | \|variation J-1\| ≥ 20 % sans actualité associée → alerte HIGH `RECO_UNCLEAR` (règle générique `P-ANOMALY`, commune à tous les agents). |
| Priorité | Tout remonte dans le mail ; ordre fixe pour l'objet et le tri (§6.6). |
| Acquittement | MVP : l'utilisateur édite la config (statut, prix d'entrée, désactivation). Phase 2 : acquittement par réponse au mail. |
| Tests | Tests unitaires (pytest) + evals sur fixtures + environnement de test isolé (SQLite séparée, objet préfixé `[TEST]`). **Pas de dry-run** : les mails de test partent réellement. |
| Ligne Nanobiotix | NANO (Euronext Paris, EUR). Pas d'ADS NBTX. |
| Horizons UBI | Sortie à horizon 1 an par défaut : `max_holding_days`. |

---

## 3. Architecture

### 3.1 Pipeline d'un run

```
cron 07:00 Europe/Paris → docker compose run --rm watcher   (python -m watcher.run --all)

ping Healthchecks /start
pour chaque agent (isolé : une exception n'arrête jamais les autres agents)
   1. Charger + valider la config          → échec : agent désactivé pour ce run + alerte config (§9.4)
   2. Ignorer l'agent si status = CLOSED
   3. Cours : ~15 dernières séances        → PriceSnapshot (dernière clôture, variation J-1, nouvelle clôture ?)
   4. Fetch des sources (plugins)          → NewsItem normalisés, filtrés : déjà vus, trop anciens
   5. Tri LLM (Haiku)                      → IDs des documents pertinents pour cet agent
   6. Analyse LLM (Sonnet)                 → Analysis(matches=[RuleMatch, ...])
   7. Résolution (code)                    → Alert : overrides, garde-fous, rumeurs, contradictions, armement
   8. Règles déterministes (code)          → price, surveillances armées, time, anomalie de prix
   9. Dédoublonnage + tri par priorité     → alertes à envoyer, écrites en base avec sent_at = NULL (outbox)
  10. Commit de l'état de l'agent          → seen_items, armed_watches, events (uniquement si l'agent a réussi)
fin pour
11. Envoi : 1 mail par agent pour ses alertes CRITICAL/HIGH ; 1 digest global si ≥ 1 alerte INFO
    → sent_at renseigné seulement après succès SMTP ; les alertes non envoyées repartent au run suivant
12. Heartbeat hebdomadaire (le lundi) ; enregistrement du run (table runs)
13. ping Healthchecks : succès si tous les agents OK, sinon /fail (avec résumé des erreurs)
```

### 3.2 Arborescence cible

```
watcher/
├── compose.yaml
├── Dockerfile
├── requirements.txt
├── .env.example
├── agents/
│   ├── _defaults.yaml          # paramètres globaux 
│   ├── ubi/
│   │   ├── config.yaml         # position, sources, règles 
│   │   └── prompt.md           # thèse, contexte, nuances d'interprétation (§7.4)
│   └── nano/
│       ├── config.yaml         
│       └── prompt.md
├── watcher/
│   ├── run.py                  # CLI : --all | --agent ID | --inject FILE --agent ID [--primary] | --baseline
│   ├── settings.py             # variables d'environnement (WATCHER_ENV, clés, SMTP...)
│   ├── config.py               # modèles de config + chargement isolé par agent
│   ├── models.py               # NewsItem, PriceSnapshot, RuleMatch, Analysis, Alert
│   ├── prices.py               # interface PriceProvider + implémentation MVP
│   ├── sources/
│   │   ├── base.py             # Protocol Fetcher + registre par type
│   │   ├── google_news.py
│   │   ├── rss.py
│   │   ├── clinicaltrials.py
│   │   ├── edgar.py
│   │   ├── dila_amf.py         # selon résultat de l'étape 0
│   │   └── html_list.py        # fallback scraping, selon étape 0
│   ├── llm/
│   │   ├── triage.py           # agent de tri
│   │   ├── analysis.py         # agent d'analyse + output_validator
│   │   └── instructions.py     # consignes globales (§7.3) + assemblage du prompt
│   ├── engine/
│   │   ├── metrics.py          # price_vs_entry, dilution_pct, figure_vs_prev_close...
│   │   ├── resolve.py          # RuleMatch → Alert
│   │   ├── price_rules.py      # règles price + surveillances armées + anomalie
│   │   ├── time_rules.py
│   │   ├── priority.py
│   │   └── dedup.py
│   ├── store.py                # SQLite : schéma, accès, migrations simples
│   ├── notify/
│   │   ├── mailer.py           # SMTP
│   │   └── templates.py        # alerte, digest, heartbeat, erreur de config
│   └── monitoring.py           # logging + Healthchecks
├── fixtures/                   # documents réels ou synthétiques étiquetés (evals, --inject)
│   ├── ubi/
│   └── nano/
├── docs/
│   └── sources.md              # livrable de l'étape 0
└── tests/
```

### 3.3 Stack

| Brique | Choix | Remarque |
|---|---|---|
| Langage | Python 3.12 | |
| Agents LLM | `pydantic-ai-slim` avec l'extra du provider (`anthropic`)| Vérifier les noms d'extras dans la doc d'installation, épingler la version |
| Validation | Pydantic v2 | |
| HTTP | `httpx` | Timeouts explicites, retries limités |
| RSS | `feedparser` | |
| Config | `PyYAML` (`safe_load` uniquement) | |
| Cours | `yfinance` en MVP, derrière une interface | Non officiel : à valider à l'étape 0 (§14) |
| Stockage, mail, logs | `sqlite3`, `smtplib`, `logging` (stdlib) | |
| Tests | `pytest` | Marqueur `eval` pour les tests qui appellent un vrai LLM |

---

## 4. Configuration

### 4.1 Principe

- `agents/_defaults.yaml` : paramètres globaux (modèles, seuils génériques, priorités, dédoublonnage).
- `agents/<id>/config.yaml` : un agent = une position + ses sources + ses règles.
- Chaque fichier est validé par Pydantic **agent par agent**. Une config invalide désactive uniquement l'agent concerné.

### 4.2 Schéma Pydantic de la config (contrat)

```python
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator


class Action(str, Enum):
    RECO_SELL_ALL = "RECO_SELL_ALL"
    RECO_SELL_HALF = "RECO_SELL_HALF"
    RECO_HOLD = "RECO_HOLD"
    RECO_UNCLEAR = "RECO_UNCLEAR"
    RECO_BUY = "RECO_BUY"
    RECO_NO_ENTRY = "RECO_NO_ENTRY"
    IGNORE = "IGNORE"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"   # mail d'alerte
    HIGH = "HIGH"           # mail d'alerte
    INFO = "INFO"           # digest (envoyé seulement s'il n'est pas vide)


Metric = Literal["price_vs_entry", "dilution_pct", "figure_vs_prev_close", "price_vs_figure", "figure"]
Op = Literal[">=", ">", "<=", "<"]
FIGURE_METRICS = {"figure_vs_prev_close", "price_vs_figure", "figure"}


class Condition(BaseModel):
    metric: Metric
    op: Op
    value: float
    figure: str | None = None  # obligatoire pour les métriques basées sur un chiffre extrait

    @model_validator(mode="after")
    def _figure_required(self) -> Condition:
        if self.metric in FIGURE_METRICS and not self.figure:
            raise ValueError(f"metric {self.metric} requiert 'figure'")
        return self


class Arm(BaseModel):
    """Surveillance persistante créée à la détection d'un événement, évaluée par le code à chaque run."""
    id: str
    when: Condition           # typiquement price_vs_figure
    action: Action
    severity: Severity


class Override(BaseModel):
    """Le premier override dont la condition est vraie remplace l'action et la sévérité par défaut."""
    id: str | None = None     # ID affiché dans le mail (ex. U-E2) ; défaut : ID de la règle
    when: Condition
    action: Action
    severity: Severity
    arms: Arm | None = None
    if_unavailable: Literal["unclear", "skip"] = "unclear"  # métrique non calculable : RECO_UNCLEAR ou override ignoré


class RuleBase(BaseModel):
    id: str
    phase: Literal["WATCH", "OWNED", "ANY"]
    direction: Literal["entry", "bullish", "bearish", "neutral"]
    action: Action
    severity: Severity
    unless_fired: list[str] = Field(default_factory=list)  # inactive si l'une de ces règles a déjà été déclenchée
    note: str | None = None   # explication affichée dans le mail pour les règles price / time


class EventRule(RuleBase):
    kind: Literal["event"]
    trigger: str                                               # texte montré au LLM
    figures: dict[str, str] = Field(default_factory=dict)      # nom → description ; chiffres à extraire
    overrides: list[Override] = Field(default_factory=list)


class PriceRule(RuleBase):
    kind: Literal["price"]
    when: Condition
    unless_armed: bool = False   # « sans offre » : inactive tant qu'une surveillance armée est active


class TimeRule(RuleBase):
    kind: Literal["time"]
    deadline: date | None = None
    max_holding_days: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one_horizon(self) -> TimeRule:
        if (self.deadline is None) == (self.max_holding_days is None):
            raise ValueError(f"{self.id} : renseigner exactement un de deadline / max_holding_days")
        if self.max_holding_days is not None and self.phase != "OWNED":
            raise ValueError(f"{self.id} : max_holding_days n'a de sens qu'en phase OWNED")
        return self


Rule = Annotated[EventRule | PriceRule | TimeRule, Field(discriminator="kind")]

SourceType = Literal["google_news_rss", "rss", "clinicaltrials", "edgar", "dila_amf", "html_list"]


class Source(BaseModel):
    name: str
    type: SourceType
    primary: bool
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)   # validés par le fetcher correspondant


class Position(BaseModel):
    ticker: str
    isin: str = Field(pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
    price_symbol: str                                       # symbole chez le fournisseur de cours
    listing: str
    currency: Literal["EUR", "USD"]
    account: Literal["CTO", "PEA"]
    budget_eur: float = Field(gt=0)
    status: Literal["WATCH", "OWNED", "CLOSED"]
    entry_price: float | None = Field(default=None, gt=0)   # devise de la ligne
    entry_date: date | None = None
    shares_outstanding: int | None = Field(default=None, gt=0)
    shares_outstanding_as_of: date | None = None

    @model_validator(mode="after")
    def _owned_requires_entry(self) -> Position:
        if self.status == "OWNED" and (self.entry_price is None or self.entry_date is None):
            raise ValueError(
                f"{self.ticker} : status OWNED sans entry_price / entry_date. "
                "Renseigne le prix et la date d'exécution réels avant de lancer l'agent."
            )
        return self


class AgentConfig(BaseModel):
    agent_id: str = Field(pattern=r"^[A-Z0-9_]+$")
    company: str
    thesis: str
    context_updated_at: date
    position: Position
    sources: list[Source]
    keywords: list[str]
    rules: list[Rule]

    @model_validator(mode="after")
    def _consistency(self) -> AgentConfig:
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("IDs de règles dupliqués")
        unknown = {ref for r in self.rules for ref in r.unless_fired} - set(ids)
        if unknown:
            raise ValueError(f"unless_fired référence des règles inconnues : {sorted(unknown)}")
        return self

    def active_rules(self, fired: set[str]) -> list[Rule]:
        """Règles applicables au statut courant, hors règles neutralisées par unless_fired."""
        if self.position.status == "CLOSED":
            return []
        return [
            r for r in self.rules
            if r.phase in (self.position.status, "ANY") and not (set(r.unless_fired) & fired)
        ]

    def event_rules(self, fired: set[str]) -> list[EventRule]:
        return [r for r in self.active_rules(fired) if isinstance(r, EventRule)]
```

`fired` est l'ensemble des `source_rule_id` présents dans la table `events` pour cet agent (historique complet). Le modèle `Defaults` est à écrire sur le même principe.

### 4.3 Métriques calculées par le code

| Métrique | Formule | Données requises | Si indisponible |
|---|---|---|---|
| `price_vs_entry` | `last_close / entry_price` | Position `OWNED`, cours | Non évaluable |
| `dilution_pct` | `100 × new_shares / (existing + new_shares)` avec `existing` = chiffre extrait `existing_shares` sinon `position.shares_outstanding`. Si `new_shares = 0` : 0, sans besoin de `existing` | `new_shares` + un des deux | Non évaluable |
| `figure_vs_prev_close` | `figures[f] / clôture de la dernière séance strictement antérieure à event_date` | Chiffre extrait, historique de cours | Non évaluable |
| `price_vs_figure` | `last_close / valeur de référence` (stockée dans `armed_watches.ref_value`) | Surveillance armée | — |
| `figure` | `figures[f]` brut | Chiffre extrait | Non évaluable |
| `daily_move_pct` (interne) | `100 × (last_close / prev_close − 1)` | 2 clôtures | Anomalie et promotion des rumeurs sautées |

Une métrique « non évaluable » lève une exception dédiée (`MetricUnavailable`) ; sa gestion est décrite en §6.1.

---

## 5. Modèles de données

### 5.1 Documents et cours

```python
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class NewsItem(BaseModel):
    id: str                         # sha256 de l'URL canonique (ou du guid RSS / accession EDGAR)
    agent_id: str
    source_name: str
    source_type: str
    source_primary: bool            # vient de la config, jamais du LLM
    url: HttpUrl
    title: str
    published_at: datetime | None   # si absent : date de première détection
    summary: str = ""               # extrait court, utilisé par le tri
    text: str | None = None         # texte complet (récupéré seulement pour les documents retenus au tri)


class PriceSnapshot(BaseModel):
    symbol: str
    last_close: float
    last_close_date: date
    prev_close: float | None
    daily_move_pct: float | None
    is_new_close: bool              # False le week-end / jours fériés : règles de prix sautées
    history: list[tuple[date, float]]
```

### 5.2 Sortie du LLM (seul contenu que le LLM a le droit de produire)

```python
class TriageResult(BaseModel):
    relevant_ids: list[str] = Field(default_factory=list)


class RuleMatch(BaseModel):
    """Ni action, ni sévérité, ni URL : uniquement l'identification d'un événement et ses preuves."""
    rule_id: str
    item_ids: list[str] = Field(min_length=1, description="IDs des documents fournis qui prouvent l'événement")
    headline: str = Field(max_length=200)
    rationale: str = Field(max_length=800)
    confidence: float = Field(ge=0, le=1)
    event_date: date
    extracted_figures: dict[str, float] = Field(default_factory=dict)


class Analysis(BaseModel):
    matches: list[RuleMatch] = Field(default_factory=list)   # liste vide = rien à signaler (cas nominal)
```

### 5.3 Alerte (construite exclusivement par le code)

```python
class Evidence(BaseModel):
    item_id: str
    url: HttpUrl
    source_name: str
    primary: bool


class Alert(BaseModel):
    agent_id: str
    rule_id: str                    # ID affiché (ID de l'override s'il y en a un)
    source_rule_id: str             # ID de la règle dans la config (sert à unless_fired et au dédoublonnage)
    origin: Literal["event", "price", "arm", "time", "anomaly"]
    action: Action
    severity: Severity
    headline: str
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)
    is_rumor: bool = False
    confidence: float | None = None           # None pour les règles purement déterministes
    event_date: date
    figures: dict[str, float] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)    # métriques calculées (dilution, prime...)
    price: PriceSnapshot | None = None
    downgrade_reason: str | None = None       # toujours affiché dans le mail quand il est renseigné
```

---

## 6. Moteur de règles (code déterministe)

### 6.1 Résolution d'un `RuleMatch` en `Alert`

Étapes, dans cet ordre :

1. **Règle** : récupérer la règle `event` par `rule_id`. Si elle n'est plus active (phase, `unless_fired`), ignorer le match et le logger (défense en profondeur : le LLM ne voit que les règles actives).
2. **Fraîcheur** : si `event_date < today − max_event_age_days`, ignorer le match et le logger. Évite qu'un article récapitulatif relance un événement ancien.
3. **Preuves** : construire les `Evidence` depuis les `NewsItem` fournis. `is_primary = any(e.primary)`, `is_rumor = not is_primary`.
4. **Overrides** : évaluer les overrides dans l'ordre (voir code ci-dessous). Le premier vrai l'emporte. Si aucun n'est vrai mais qu'au moins un override en `if_unavailable: unclear` était non évaluable, on applique `missing_figures` (`RECO_UNCLEAR`, HIGH, raison « chiffres non extraits, lis la source »). Sinon, on garde l'action et la sévérité par défaut.
5. **Garde-fou actionnable** : si l'action est dans `actionable_actions` (`SELL_ALL`, `SELL_HALF`, `BUY`) et que `is_rumor` est vrai **ou** que `confidence < min_confidence_actionable`, alors l'action devient `RECO_UNCLEAR` et `downgrade_reason` est renseigné.
6. **Rumeur** : si `is_rumor`, la sévérité passe à `rumor.severity` (INFO), promue à HIGH si `|daily_move_pct| ≥ 10`. Une rumeur n'est **jamais** CRITICAL.
7. **Armement** : si l'override retenu a un `arms` **et** que le match n'est ni une rumeur ni rétrogradé, créer une ligne `armed_watches` avec `ref_value = figures[arms.when.figure]`. La surveillance est évaluée immédiatement dans le même run (§6.3).
8. **Contradictions** (après résolution de tous les matches du run) : si deux matches partagent au moins un `item_id` et ont des directions opposées (`bullish` / `bearish`), les deux deviennent `RECO_UNCLEAR` avec la raison « signaux contradictoires sur le même document ».

```python
class MetricUnavailable(Exception):
    """Une métrique ne peut pas être calculée (chiffre non extrait, cours manquant...)."""


def pick_outcome(rule: EventRule, ctx: MetricContext) -> Outcome:
    not_evaluable: list[str] = []
    for ov in rule.overrides:
        try:
            if ctx.check(ov.when):
                return Outcome.from_override(rule, ov)
        except MetricUnavailable as exc:
            if ov.if_unavailable == "unclear":
                not_evaluable.append(str(exc))
    if not_evaluable:
        return Outcome.missing_figures(rule, reasons=not_evaluable)
    return Outcome.default(rule)
```

### 6.2 Règles `price`

- Évaluées seulement si `status = OWNED` et `is_new_close = True`.
- `unless_armed: true` : la règle est inactive tant qu'une surveillance armée est active pour l'agent (cas « doublement du cours sans offre »).
- Dédoublonnage : une seule alerte par couple `(agent, rule_id, entry_date)`. Un nouveau prix d'entrée (nouvelle position) réarme la règle.

### 6.3 Surveillances armées

- Créées à l'étape 7 de §6.1 ; statut `active` → `fired` (alerte envoyée) ou `cancelled` (position passée en `CLOSED`).
- Évaluées à chaque run où `is_new_close = True`, y compris immédiatement lors du run qui les crée : une offre annoncée en séance peut avoir déjà porté le cours au-dessus du seuil.
- Une surveillance déclenchée produit une alerte `origin = "arm"` avec l'ID de l'`Arm` (ex. `U-B1-EXIT`).

### 6.4 Règles `time`

- `deadline` : déclenchée quand `today ≥ deadline`.
- `max_holding_days` : déclenchée quand `today ≥ entry_date + max_holding_days`.
- `unless_fired` s'applique (ex. pas de time stop « aucun résultat » si un résultat a déjà été détecté).
- Une seule alerte par couple `(agent, rule_id)`, jamais répétée.

### 6.5 Anomalie de prix (`P-ANOMALY`, générique)

Déclenchée si toutes les conditions suivantes sont réunies :

- `is_new_close = True` ;
- `|daily_move_pct| ≥ price_anomaly.abs_move_pct` (20 %) ;
- **aucun match** pour cet agent dans ce run (y compris les matches `IGNORE`, qui « expliquent » le mouvement) **et** aucun événement envoyé pour cet agent depuis `quiet_days` jours.

Elle produit une alerte HIGH `RECO_UNCLEAR` : « mouvement inexpliqué de X %, cherche la source ». Elle s'applique en `WATCH` comme en `OWNED`. Une seule alerte par date de clôture.

### 6.6 Priorité et consolidation

- Ordre des actions : `RECO_SELL_ALL > RECO_SELL_HALF > RECO_UNCLEAR > RECO_BUY > RECO_NO_ENTRY > RECO_HOLD > IGNORE`.
- Tri des alertes : sévérité (`CRITICAL > HIGH > INFO`), puis action selon l'ordre ci-dessus.
- Toutes les alertes remontent. L'objet du mail d'un agent reprend l'alerte de tête.
- Les matches `IGNORE` ne sont jamais envoyés : ils sont seulement journalisés (et servent à l'anomalie de prix).

### 6.7 Dédoublonnage

**Niveau 1 : documents.** Un couple `(agent_id, item_id)` déjà présent dans `seen_items` n'est ni retrié ni réanalysé. Les documents sont marqués vus uniquement quand l'agent a terminé son run avec succès : en cas d'erreur (API LLM indisponible par exemple), ils seront retraités le lendemain.

**Niveau 2 : événements.**

| Origine | Clé | Règle |
|---|---|---|
| `event` | `(agent, rule_id affiché)` | Supprimée si un événement de même clé existe depuis moins de `event_window_days` (30 j), **sauf escalade** : passage rumeur → source primaire, sévérité supérieure, ou action plus prioritaire. |
| `price` | `(agent, rule_id, entry_date)` | Une fois par position. |
| `arm`, `time` | `(agent, rule_id)` | Une fois. |
| `anomaly` | `(agent, date de clôture)` | Une fois par clôture. |

Limitation assumée : deux événements distincts de la même règle à moins de 30 jours d'écart → le second est supprimé. Il reste journalisé.

### 6.8 Outbox

Les alertes à envoyer sont écrites dans `events` avec `sent_at = NULL`. `sent_at` est renseigné après succès SMTP. Au début de l'étape d'envoi, toutes les alertes `sent_at IS NULL` (y compris celles des runs précédents) sont envoyées.

---

## 7. Couche LLM

### 7.1 Agent de tri (Haiku)

- **Rôle** : réduire le bruit avant l'analyse. Priorité au **rappel** : en cas de doute, le document est gardé.
- **Entrée** : pour chaque document, son ID, sa source, sa date, son titre et un résumé tronqué à 500 caractères. Lots de `triage_batch_size` documents.
- **Consignes** : société, résumé de la thèse, mots-clés, liste des déclencheurs des règles `event` actives (texte seul).
- **Sortie** : `TriageResult`. Un `output_validator` vérifie que tous les IDs renvoyés existent (sinon `ModelRetry`).
- Le texte complet (`NewsItem.text`) n'est récupéré par le fetcher **que** pour les documents retenus.

### 7.2 Agent d'analyse (Sonnet)

- **Consignes** : consignes globales (§7.3) + `agents/<id>/prompt.md` + liste des règles `event` actives (ID, déclencheur, chiffres attendus avec leur description).
- **Message** : contexte de cours (dernière clôture, variation J-1, historique court) + documents retenus (ID, source, date, titre, texte tronqué à `max_doc_chars`).
- **Sortie** : `Analysis`.
- `retries=2`. Si l'analyse échoue après les retries, l'agent est en échec pour ce run : documents non marqués vus, erreur remontée à Healthchecks (`/fail`) et au heartbeat.
- Le modèle est passé au moment du run depuis la config, pas à la construction de l'agent. C'est ce qui permet de basculer de modèle par config et de surcharger le modèle pour les evals.

Validation des références (API PydanticAI actuelle : `output_validator` + `ModelRetry`) :

```python
from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext


@dataclass
class AnalysisDeps:
    cfg: AgentConfig
    rules: list[EventRule]          # règles event actives, déjà filtrées
    items: dict[str, NewsItem]      # documents fournis, indexés par ID


analyst = Agent(deps_type=AnalysisDeps, output_type=Analysis, retries=2)


@analyst.output_validator
def check_references(ctx: RunContext[AnalysisDeps], out: Analysis) -> Analysis:
    valid_rules = {r.id for r in ctx.deps.rules}
    for m in out.matches:
        if m.rule_id not in valid_rules:
            raise ModelRetry(f"rule_id inconnu : {m.rule_id}. IDs valides : {sorted(valid_rules)}")
        if unknown := [i for i in m.item_ids if i not in ctx.deps.items]:
            raise ModelRetry(f"item_ids inconnus : {unknown}")
    return out
```

**Point de vigilance important** : le validateur ne doit **pas** exiger la présence des chiffres (`extracted_figures`). Forcer un retry sur un chiffre absent du document pousserait le modèle à en inventer un. Un chiffre manquant est géré par le code (`missing_figures` → `RECO_UNCLEAR`).

### 7.3 Consignes globales de l'agent d'analyse (à intégrer telles quelles, en français)

```text
Tu es un agent de veille boursière 100 % consultatif. Tu n'as aucun accès au portefeuille ni au courtier.
Ta seule tâche : lire les documents fournis et identifier ceux qui décrivent un événement correspondant
à l'une des règles listées.

Règles de sortie :
- Pour chaque événement correspondant à une règle, renvoie un match avec l'ID exact de la règle
  et les IDs des documents qui le prouvent.
- Si aucun document ne correspond à une règle, renvoie une liste vide. C'est le cas le plus fréquent
  et c'est une réponse correcte.
- Ne décide jamais d'une action ni d'une sévérité : elles sont déterminées ailleurs.
- Ne fais aucun calcul (pourcentage, dilution, prime, conversion de devise). Extrais uniquement
  les chiffres bruts demandés par la règle, tels qu'écrits dans le document, convertis en nombre
  (ex. « 40 millions d'actions » → 40000000).
- Si un chiffre demandé n'apparaît pas explicitement dans le document, omets la clé.
  N'estime jamais un chiffre absent.
- event_date est la date de l'événement annoncé (date du communiqué), pas la date de l'article qui le relaie.
- Un article de presse peut correspondre à une règle : signale-le normalement.
- Si la formulation est ambiguë ou si un document semble correspondre à deux règles incompatibles,
  choisis la règle la plus prudente indiquée dans les consignes de l'agent et explique l'ambiguïté.
- confidence mesure ta certitude que l'événement décrit correspond exactement au déclencheur de la règle,
  pas la probabilité que l'information soit vraie.
- rationale : en français, 3 phrases maximum, en citant le passage clé du document.
```

### 7.4 Fichiers `agents/<id>/prompt.md`

À dériver des sections 1 et 2 de `watch_agents_spec.md` (v1.2) :

- **Garder** : thèse, contexte chiffré (daté), nuances d'interprétation (« pourquoi pas de ratio dette nette / EBITDA » pour UBI, « point d'attention p-value » pour NANO), description de l'étude NANORAY-312.
- **Supprimer** : tous les tableaux de règles et la section 0. La source de vérité des règles est `config.yaml` ; les règles sont injectées automatiquement dans le prompt.
- **Ajouter pour NANO** :
  - En cas de doute entre N-B1 (succès net) et N-S5 (résultat mitigé), choisir N-S5.
  - Les publications mensuelles « nombre total de droits de vote et d'actions composant le capital » sont du bruit : ne pas les faire matcher (elles servent seulement à mettre à jour `shares_outstanding` à la main).
- **Ajouter pour UBI** : un report de jeu qui s'accompagne d'une révision de guidance relève de U-S3, pas de U-N1.

### 7.5 Coûts et garde-fous

- Estimation : de l'ordre de quelques dollars par mois pour les deux agents, linéaire par agent ajouté.
- Chaque appel LLM est plafonné via les `UsageLimits` de PydanticAI. Le run complet est plafonné par `llm_budget.max_total_tokens_per_run` : au-delà, le run s'arrête proprement et passe en échec.
- La consommation de tokens (renvoyée par PydanticAI après chaque run d'agent) est enregistrée dans `runs` et affichée dans le heartbeat hebdomadaire.
- Surcharge des modèles par variables d'environnement (`WATCHER_MODEL_TRIAGE`, `WATCHER_MODEL_ANALYSIS`) pour les evals et les comparaisons de providers.

---

## 8. Sources et cours

### 8.1 Interface des fetchers

```python
from datetime import datetime
from typing import Protocol


class Fetcher(Protocol):
    source_type: str

    def validate_params(self, params: dict) -> None:
        """Lève ValueError si les paramètres de la source sont invalides (appelé au chargement de la config)."""

    def fetch(self, source: Source, agent: AgentConfig, since: datetime) -> list[NewsItem]:
        """Retourne les documents publiés depuis `since` (sans le texte complet)."""

    def fetch_text(self, item: NewsItem) -> str:
        """Récupère le texte complet d'un document retenu au tri."""
```

Registre par `type` : ajouter un type de source = ajouter un module, sans toucher au reste. Règles communes à tous les fetchers :

- timeouts explicites ;
- 2 retries maximum ;
- `User-Agent` explicite ;
- une source en erreur est loggée et remontée dans le heartbeat, mais ne fait pas échouer l'agent : les autres sources suffisent pour ce run.

Documents plus anciens que `max_item_age_days` : ignorés.

### 8.2 Types de sources MVP

| Type | Primaire | Paramètres | Comportement |
|---|---|---|---|
| `google_news_rss` | non | `query`, `hl`, `gl`, `ceid` | Flux de recherche Google News. Format d'URL et opérateurs de requête à valider à l'étape 0. |
| `rss` | selon config | `url` | Flux RSS générique (pages investisseurs si elles en proposent). |
| `clinicaltrials` | oui | `nct_id` | API v2 ClinicalTrials.gov (`/api/v2/studies/{nct_id}`). **Détection de changement** : instantané des champs clés (statut global, dates de fin principale et totale, présence de résultats, date de dernière mise à jour, critères d'évaluation principaux) stocké dans `source_state`. Toute différence produit un `NewsItem` synthétique dont le texte est le diff. Premier run = référence, sans document. |
| `edgar` | oui | `cik`, `forms` | API JSON des soumissions SEC. `User-Agent` avec contact obligatoire (politique SEC). Récupère le document principal / l'exhibit 99.1 des formulaires listés. |
| `dila_amf` | oui | 🔲 (étape 0) | Open data AMF diffusé par la DILA. Format, fréquence et filtrage par ISIN à investiguer. |
| `html_list` | selon config | `url`, sélecteurs CSS | Fallback de scraping d'une page de communiqués, uniquement s'il n'existe ni RSS ni API. |

Nanobiotix dépose une large partie de ses communiqués en formulaire 6-K (exhibit 99.1) : EDGAR est probablement un substitut fiable à sa page investisseurs. À confirmer à l'étape 0.

### 8.3 Cours

```python
class PriceProvider(Protocol):
    def history(self, symbol: str, sessions: int) -> list[tuple[date, float]]:
        """Clôtures des `sessions` dernières séances, triées par date croissante."""
```

- Implémentation MVP : `yfinance` (symboles `UBI.PA`, `NANO.PA`).
- Non officiel, donc fragile : à valider à l'étape 0, avec identification d'un fournisseur de repli (🔲) implémentable derrière la même interface.
- `is_new_close = last_close_date > dernière clôture déjà traitée` (stockée dans `source_state`).
- En cas d'échec du provider : règles de prix et anomalie sautées pour ce run, avertissement dans le heartbeat. L'analyse des documents continue.

---

## 9. Notifications

Toutes en texte brut (UTF-8), envoyées via Gmail SMTP (`smtp.gmail.com`, port 587, STARTTLS). En environnement de test, tous les objets sont préfixés `[TEST]`.

### 9.1 Mail d'alerte (1 par agent et par run, s'il existe au moins une alerte CRITICAL / HIGH)

Objet : `[CRITICAL] NANO · Vendre toute la ligne · <headline de l'alerte de tête>`

Corps, pour chaque alerte (triées selon §6.6) :

- recommandation en clair (libellés ci-dessous) + sévérité ;
- règle : ID affiché + texte du déclencheur (depuis la config) ;
- `rationale` du LLM, ou explication générée par le code pour les règles `price`, `time`, `arm` et `anomaly` ;
- chiffres extraits et métriques calculées (ex. « dilution calculée : 22,0 % ; seuil : 20 % ») ;
- contexte de cours : dernière clôture, variation J-1, écart au prix d'entrée si `OWNED` ;
- preuves : liens, chacun marqué `[source primaire]` ou `[RUMEUR — presse]` ;
- `downgrade_reason` s'il est renseigné (« rétrogradé en RECO_UNCLEAR : source non primaire ») ;
- confiance du LLM si applicable.

Pied de mail fixe : « Recommandation automatique issue de tes règles. Aucune opération n'a été exécutée. »

| Code | Libellé dans le mail |
|---|---|
| `RECO_SELL_ALL` | Je te recommande de vendre toute la ligne |
| `RECO_SELL_HALF` | Je te recommande de vendre la moitié (récupérer la mise) |
| `RECO_HOLD` | Rien à faire, information pour le suivi |
| `RECO_BUY` | Les conditions d'entrée sont réunies, l'achat est envisageable |
| `RECO_NO_ENTRY` | Les conditions d'entrée ne sont pas réunies, ne pas acheter |
| `RECO_UNCLEAR` | Situation ambiguë : pas de recommandation, lis la source toi-même |

### 9.2 Digest INFO (1 global par run, seulement s'il contient au moins un élément)

Objet : `[INFO] Veille du JJ/MM — N éléments`. Une section par agent, une ligne par alerte (règle, headline, lien principal, tag rumeur).

### 9.3 Heartbeat hebdomadaire (le lundi, après le run)

Objet : `[HEARTBEAT] Semaine du JJ/MM — X/7 runs OK, Y alertes`. Contenu :

- runs des 7 derniers jours (statut, durée) ;
- alertes envoyées par agent ;
- statut de chaque agent (`WATCH` / `OWNED` / `CLOSED` / désactivé pour config invalide) ;
- sources en erreur ;
- surveillances armées actives ;
- tokens consommés sur 7 jours et coût estimé ;
- avertissements :
  - `context_updated_at` datant de plus de 90 jours ;
  - `shares_outstanding` absent ou ancien ;
  - sources en `enabled: false` ;
  - time stops à moins de 30 jours.

### 9.4 Erreur de configuration

Un mail immédiat à la **première** détection d'une erreur (dédoublonnée sur l'empreinte du message d'erreur), puis rappel dans chaque heartbeat tant qu'elle persiste. Exemple : `NANO` en `OWNED` sans `entry_price` → agent désactivé, mail « renseigne entry_price et entry_date ».

---

## 10. Persistance (SQLite)

Base : `data/<env>/watcher.sqlite`. Schéma créé au démarrage s'il n'existe pas ; table `schema_version` pour les migrations simples.

```sql
CREATE TABLE IF NOT EXISTS seen_items (
    agent_id        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TEXT,
    triage_relevant INTEGER,              -- NULL si non trié (baseline)
    first_seen_at   TEXT NOT NULL,
    PRIMARY KEY (agent_id, item_id)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    rule_id         TEXT NOT NULL,        -- ID affiché
    source_rule_id  TEXT NOT NULL,        -- ID de la règle en config (unless_fired)
    origin          TEXT NOT NULL,        -- event | price | arm | time | anomaly
    action          TEXT NOT NULL,
    severity        TEXT NOT NULL,
    is_rumor        INTEGER NOT NULL,
    dedup_key       TEXT NOT NULL,
    event_date      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    sent_at         TEXT,                 -- NULL = en attente d'envoi (outbox)
    payload_json    TEXT NOT NULL         -- Alert sérialisée
);
CREATE INDEX IF NOT EXISTS idx_events_dedup ON events (agent_id, dedup_key, created_at);

CREATE TABLE IF NOT EXISTS armed_watches (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    arm_id          TEXT NOT NULL,
    source_event_id INTEGER NOT NULL REFERENCES events(id),
    metric          TEXT NOT NULL,
    ref_value       REAL NOT NULL,
    op              TEXT NOT NULL,
    threshold       REAL NOT NULL,
    action          TEXT NOT NULL,
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL,        -- active | fired | cancelled
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    agent_id        TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    state_json      TEXT NOT NULL,        -- instantané ClinicalTrials, dernière clôture traitée...
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (agent_id, source_name)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY,
    env             TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,        -- running | ok | partial | failed
    agents_ok       INTEGER NOT NULL DEFAULT 0,
    agents_failed   INTEGER NOT NULL DEFAULT 0,
    alerts_created  INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    errors_json     TEXT
);

CREATE TABLE IF NOT EXISTS config_errors (
    agent_id        TEXT NOT NULL,
    error_hash      TEXT NOT NULL,
    message         TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    notified_at     TEXT,
    resolved_at     TEXT,
    PRIMARY KEY (agent_id, error_hash)
);
```

---

## 11. Infra et déploiement

### 11.1 Conteneur

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY watcher/ watcher/
RUN useradd --create-home --uid 1000 watcher
USER watcher
```

```yaml
# compose.yaml
services:
  watcher:
    build: .
    env_file: .env
    environment:
      TZ: Europe/Paris
    volumes:
      - ./data:/app/data            # SQLite + logs : survivent aux rebuilds
      - ./agents:/app/agents:ro     # configs éditables sans rebuild
      - ./fixtures:/app/fixtures:ro
    command: ["python", "-m", "watcher.run", "--all"]
```

Le conteneur ne tourne pas en permanence : il est lancé par le cron de l'hôte, puis supprimé (`--rm`).

### 11.2 Planification sur le VPS

```bash
sudo timedatectl set-timezone Europe/Paris
# crontab -e (utilisateur de déploiement)
0 7 * * * cd /opt/watcher && docker compose run --rm watcher >> /opt/watcher/data/cron.log 2>&1
```

### 11.3 Variables d'environnement (`.env.example`, jamais commité avec des valeurs)

```dotenv
WATCHER_ENV=prod                    # prod | test
ANTHROPIC_API_KEY=
WATCHER_MODEL_TRIAGE=               # optionnel : surcharge de _defaults.yaml
WATCHER_MODEL_ANALYSIS=             # optionnel
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_APP_PASSWORD=
MAIL_TO=
HEALTHCHECKS_URL=                   # URL de ping du check (sans suffixe)
SEC_USER_AGENT=                     # ex. "watcher-perso prenom.nom@exemple.com"
```

### 11.4 Environnement de test

`WATCHER_ENV=test` implique :

- base séparée `data/test/watcher.sqlite` ;
- objets de mail préfixés `[TEST]` ;
- ping Healthchecks désactivé (ou check dédié).

Les mails partent réellement (pas de dry-run). La commande `--inject` force toujours l'environnement de test, pour ne jamais polluer l'état de prod.

### 11.5 Monitoring

- **Healthchecks.io** : check « Cron », expression `0 7 * * *`, fuseau Europe/Paris, période de grâce de 2 h.
  - Ping `/start` en début de run.
  - Ping de l'URL simple en fin de run si tous les agents sont OK.
  - Ping `/fail` sinon, avec un résumé des erreurs dans le corps de la requête.
- **Logs** : fichier rotatif `data/<env>/logs/watcher.log`, conservé 30 jours, une ligne par étape et par agent (documents fetchés, retenus au tri, matches, alertes, tokens).

### 11.6 Sécurité du VPS (minimum)

- Accès SSH par clé uniquement ; connexion root et mot de passe désactivés.
- Pare-feu : tout entrant refusé sauf SSH. Le watcher n'expose **aucun** port (trafic sortant uniquement).
- Mises à jour de sécurité automatiques.
- `.env` en `chmod 600`, hors du dépôt git.

### 11.7 Premier démarrage

`python -m watcher.run --all --baseline` : fetch de toutes les sources et marquage de tous les documents actuels comme vus, **sans tri, sans analyse, sans mail**. Initialise aussi les instantanés `source_state` (ClinicalTrials, dernière clôture). Sans cette étape, le premier run analyserait l'historique des flux et produirait des alertes sur de vieux événements.

---

## 12. Tests et evals

### 12.1 Tests unitaires (sans réseau, sans appel LLM, lancés par défaut)

| Zone | Cas minimum |
|---|---|
| Config | Chargement ; `OWNED` sans prix d'entrée → erreur ; ID dupliqué ; `unless_fired` inconnu ; `TimeRule` invalide ; un agent invalide n'empêche pas le chargement des autres. |
| Métriques | Chaque métrique de §4.3, cas nominal + `MetricUnavailable`. Exemple de référence : 40 M d'actions nouvelles pour 142 M existantes → dilution de 21,98 %. |
| Résolution | Chaque chemin d'override des config ; chiffres manquants → `RECO_UNCLEAR` HIGH ; garde-fou actionnable (rumeur, confiance < 0,8) ; rumeur INFO puis promue HIGH à ±10 % ; contradiction sur un même document ; armement seulement si source primaire ; match trop ancien ignoré. |
| Règles déterministes | `price` (dont `unless_armed`) ; surveillances armées (création, déclenchement immédiat, annulation) ; `time` (deadline, max_holding_days, unless_fired) ; anomalie (seuil, `quiet_days`, match IGNORE qui neutralise). Dates injectées (jamais `date.today()` en dur). |
| Priorité, dédoublonnage | Tri ; suppression dans la fenêtre de 30 jours ; escalade rumeur → primaire ; outbox (alerte non envoyée renvoyée au run suivant). |
| Fetchers | Sur réponses enregistrées dans `tests/data/` (RSS, JSON ClinicalTrials, JSON EDGAR) ; diff ClinicalTrials. |
| Plomberie LLM | `TestModel` / `FunctionModel` de PydanticAI : retry sur `rule_id` inconnu, retry sur `item_id` inconnu, liste vide acceptée. |

Objectif : couverture ≥ 90 % sur `watcher/engine/`.

### 12.2 Evals (appels LLM réels, marqueur `eval`, lancées à la demande : `pytest -m eval`)

Format d'une fixture : `fixtures/<agent>/<cas>.md` avec en-tête YAML.

```markdown
---
agent: NANO
source_name: SEC EDGAR
source_primary: true
synthetic: false
expected_rule_ids: [N-S4]
expected_figures: {new_shares: 3000000}
---
Texte intégral du communiqué...
```

Jeu minimum (≈ 10 cas par agent), à constituer à partir de vrais communiqués passés, complétés par des cas **synthétiques** (marqués `synthetic: true`) pour les événements qui ne se sont pas encore produits.

- **NANO** :
  - publication mensuelle des droits de vote (attendu : aucun match) ;
  - levée de fonds passée (N-S4 + `new_shares`) ;
  - données de phase 1/2 dans une autre indication (N-N2) ;
  - synthétiques : critère principal atteint (N-B1), non atteint (N-S1), résultat mitigé (N-S5), clinical hold (N-S2), résiliation J&J (N-S3), offre de rachat (N-B3 + `offer_price`).
- **UBI** :
  - communiqué sur l'opération Tencent / Vantage Studios ;
  - report d'un jeu sans révision de guidance (U-N1) ;
  - révision de guidance de FCF (U-S3 + chiffres) ;
  - article de pur gaming (attendu : aucun match) ;
  - synthétiques : refinancement sans dilution (U-E1 avec `new_shares: 0`), refinancement avec augmentation de capital (U-E1 + `new_shares`), OPA (U-B1 + `offer_price`), bris de covenant (U-S2).

Critères d'acceptation :

- **zéro faux négatif** sur les cas qui mènent à `RECO_SELL_ALL` avec source primaire ;
- **zéro faux positif** de vente sur les cas négatifs ;
- chiffres extraits exacts sur tous les cas qui en attendent.

La sortie est un tableau récapitulatif par cas et par modèle, ce qui permet de comparer Haiku, Sonnet et Gemini via `WATCHER_MODEL_ANALYSIS`.

### 12.3 Injection de bout en bout

```bash
docker compose run --rm watcher python -m watcher.run --agent NANO --inject fixtures/nano/endpoint_met.md --primary
```

Fait passer le document par le tri, l'analyse, la résolution et l'envoi, avec un vrai mail `[TEST]`. L'environnement de test est forcé.

---

## 13. Plan de réalisation

| Étape | Contenu | Critère de fin | Complexité |
|---|---|---|---|
| **0. Spike sources et cours** (1 à 2 soirs, timeboxé) | Pour chaque source des fichiers de config des agents : URL, format, fréquence, authentification, limites, exemple réel de document. Fournisseur de cours validé et repli identifié. Réponses aux 🔲 de la §15 qui relèvent de l'investigation. | `docs/sources.md` rédigé ; chaque source passée en `enabled: true` ou justifiée en `false`. | Simple |
| **1. Socle** | Squelette du dépôt, `settings`, chargement et validation de config isolés par agent, store SQLite, CLI (`--all`, `--agent`, `--baseline`), logging, Healthchecks, mailer. | Un mail `[TEST]` reçu ; config des annexes chargée ; NANO désactivé proprement tant que `entry_price` est vide. | Simple |
| **2. Moteur déterministe** (sans LLM) | Métriques, résolution (sur des `RuleMatch` construits à la main), règles `price`, surveillances armées, `time`, anomalie, priorité, dédoublonnage, outbox. | Tests unitaires de la §12.1 verts ; couverture ≥ 90 % sur `engine/`. | Moyen |
| **3. Fetchers et cours** | `google_news_rss`, `clinicaltrials`, `edgar`, provider de cours, puis `rss` / `dila_amf` / `html_list` selon l'étape 0. | Tests sur réponses enregistrées ; `--baseline` réel exécuté sans erreur. | Moyen |
| **4. Couche LLM** | Agents de tri et d'analyse, validateurs, assemblage des consignes et des `prompt.md`, `--inject`, fixtures, evals. | Critères de la §12.2 atteints avec les modèles par défaut. | Moyen à élevé |
| **5. Notifications** | Gabarits alerte, digest, heartbeat, erreur de config. | Chaque gabarit reçu en `[TEST]` via `--inject` ou en forçant le jour du heartbeat. | Simple |
| **6. Déploiement** | Dockerfile, Compose, VPS (sécurité §11.6), cron, Healthchecks. Une semaine en `WATCHER_ENV=test`, puis bascule en prod après un `--baseline`. | 7 runs consécutifs OK dans Healthchecks ; heartbeat reçu. | Simple à moyen |

### Phase 2 (backlog, non prioritaire)

- Acquittement par réponse au mail (lecture IMAP) : `SELL_HALF fait`, `CLOSED`, etc.
- Anomalie relative au marché : mouvement de NANO comparé à un indice biotech, d'UBI comparé à son indice de référence.
- Variation cumulée sur plusieurs séances (au-delà de la variation J-1).
- Tool use : outil `fetch_linked_document(url)` pour que l'agent d'analyse suive un lien cité dans un communiqué.
- Mise à jour automatique de `shares_outstanding` depuis les publications mensuelles de droits de vote.
- Nouvel agent Take-Two (validation du caractère « machine à agents »).

---

## 14. Risques

| Risque | Impact | Mitigation |
|---|---|---|
| Pas de flux propre pour certaines pages investisseurs ou pour l'AMF | Événements détectés seulement via la presse (rumeur), donc jamais actionnables | Étape 0 ; EDGAR pour Nanobiotix ; scraping `html_list` en dernier recours ; sources manquantes signalées dans le heartbeat |
| `yfinance` non officiel, peut casser | Règles de prix et anomalie inopérantes | Interface `PriceProvider`, repli identifié à l'étape 0, avertissement dans le heartbeat |
| Faux négatif du LLM sur un événement critique | Recommandation de vente manquée | Tri orienté rappel ; evals à zéro faux négatif ; anomalie de prix comme filet de sécurité (un échec de phase 3 fait bouger le cours) |
| Faux positif de vente | Vente injustifiée si l'utilisateur suit le mail | Source primaire + confiance ≥ 0,8 obligatoires ; `RECO_UNCLEAR` par défaut ; preuves et passage cité dans le mail |
| LLM qui invente un chiffre | Seuil de dilution ou prime faussés | Consigne « omets la clé » ; pas de retry sur chiffre absent ; chiffres affichés dans le mail pour contrôle |
| Offre exprimée dans une autre devise ou par ADS | Prime non calculable | `offer_price` limité à l'EUR par action ; sinon `missing_figures` → `RECO_UNCLEAR` |
| Panne silencieuse (VPS, cron, clé expirée) | Aucun mail, et c'est indiscernable du silence normal | Healthchecks (alerte si pas de ping sous 26 h), ping `/fail`, heartbeat hebdomadaire |
| Contexte des prompts périmé | Mauvaise interprétation (ex. guidance obsolète) | `context_updated_at` + avertissement au-delà de 90 jours |
| Dérive de coût | Facture API | `UsageLimits`, plafond par run, tokens suivis dans le heartbeat |
| Serveur arrêté plus de `max_item_age_days` | Documents de la période manqués | Limitation assumée ; relancer manuellement avec un `max_item_age_days` élargi si nécessaire |
| Dédoublonnage sur 30 jours | Second événement de même règle masqué | Limitation assumée, événement journalisé |

---

## 15. À compléter (🔲)

### Par l'utilisateur, avant la mise en prod

| Élément | Où | Remarque |
|---|---|---|
| Comptes et secrets | `.env` | Clé API Anthropic (Console, facturation distincte de l'abonnement Claude), mot de passe d'application Gmail, check Healthchecks, VPS |

### À investiguer par Claude Code (étape 0)

- Flux des communiqués d'Ubisoft (RSS, API ou page à scraper).
- Format et filtrage par ISIN de l'open data AMF (DILA).
- Flux des communiqués de Johnson & Johnson.
- Couverture des communiqués de Nanobiotix par les 6-K EDGAR.
- Format d'URL et opérateurs de requête de Google News RSS.
- Fournisseur de cours de repli.

---
