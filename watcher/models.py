"""Modèles de données échangés dans le pipeline (cadrage §5).

- `NewsItem`, `PriceSnapshot` : entrées normalisées (fetchers, provider de cours).
- `TriageResult`, `RuleMatch`, `Analysis` : seul contenu que le LLM a le droit de produire.
- `Evidence`, `Alert` : construits exclusivement par le code (moteur de règles).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

from watcher.config import Action, Severity


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


# --------------------------------------------------------------------------- sortie du LLM


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


# --------------------------------------------------------------------------- alerte (code)


class Evidence(BaseModel):
    item_id: str
    url: HttpUrl
    source_name: str
    primary: bool


AlertOrigin = Literal["event", "price", "arm", "time", "anomaly"]


class ConditionCheck(BaseModel):
    """Condition évaluée par le code (override, règle price, surveillance) : affichée avec son seuil dans le mail."""

    metric: str                     # libellé de la métrique : `dilution_pct`, `figure_vs_prev_close(offer_price)`...
    value: float
    op: Literal[">=", ">", "<=", "<"]
    threshold: float
    met: bool


class Alert(BaseModel):
    agent_id: str
    rule_id: str                    # ID affiché (ID de l'override s'il y en a un)
    source_rule_id: str             # ID de la règle dans la config (unless_fired, dédoublonnage)
    origin: AlertOrigin
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
    # Contexte figé à la création de l'alerte, pour que le mail (éventuellement renvoyé depuis l'outbox) ne dépende
    # pas de la config du jour. Écart assumé avec le cadrage §5.3 : champs optionnels, ajoutés à l'étape 5.
    rule_text: str | None = None              # déclencheur (event) ou condition (price, time, arm, anomaly)
    checks: list[ConditionCheck] = Field(default_factory=list)
    currency: str | None = None
    entry_price: float | None = None          # position OWNED : écart au prix d'entrée dans le mail
