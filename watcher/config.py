"""Modèles de configuration (cadrage §4.2) et chargement isolé agent par agent.

`agents/_defaults.yaml` est global : s'il est invalide, le run entier échoue.
`agents/<id>/config.yaml` est validé séparément : une config invalide désactive uniquement cet agent.
`agents/<id>/prompt.md` (consignes rédactionnelles de l'agent d'analyse, cadrage §7.4) est chargé avec la config :
absent ou vide, il rend la config invalide.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

log = logging.getLogger(__name__)

DEFAULTS_FILE = "_defaults.yaml"
AGENT_CONFIG_FILE = "config.yaml"
AGENT_PROMPT_FILE = "prompt.md"


class ConfigError(Exception):
    """Configuration invalide (fichier absent, YAML illisible, schéma non respecté)."""


class _Strict(BaseModel):
    """Refuse les clés inconnues : une faute de frappe dans le YAML doit être une erreur, pas un oubli silencieux."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- énumérations


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


# --------------------------------------------------------------------------- règles


class Condition(_Strict):
    metric: Metric
    op: Op
    value: float
    figure: str | None = None  # obligatoire pour les métriques basées sur un chiffre extrait

    @model_validator(mode="after")
    def _figure_required(self) -> Condition:
        if self.metric in FIGURE_METRICS and not self.figure:
            raise ValueError(f"metric {self.metric} requiert 'figure'")
        return self


class Arm(_Strict):
    """Surveillance persistante créée à la détection d'un événement, évaluée par le code à chaque run."""

    id: str
    when: Condition           # price_vs_figure : cours rapporté au chiffre de référence stocké à l'armement
    action: Action
    severity: Severity

    @model_validator(mode="after")
    def _price_vs_figure(self) -> Arm:
        # armed_watches.ref_value est obligatoire, et toute autre métrique serait constante d'un run à l'autre.
        if self.when.metric != "price_vs_figure":
            raise ValueError(f"{self.id} : une surveillance armée doit utiliser la métrique price_vs_figure")
        return self


class Override(_Strict):
    """Le premier override dont la condition est vraie remplace l'action et la sévérité par défaut."""

    id: str | None = None     # ID affiché dans le mail (ex. U-E2) ; défaut : ID de la règle
    when: Condition
    action: Action
    severity: Severity
    arms: Arm | None = None
    if_unavailable: Literal["unclear", "skip"] = "unclear"  # métrique non calculable : RECO_UNCLEAR ou override ignoré


class RuleBase(_Strict):
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

    @model_validator(mode="after")
    def _figures_declared(self) -> EventRule:
        """Un chiffre référencé par un override ou une surveillance doit être demandé au LLM."""
        referenced = {
            cond.figure
            for ov in self.overrides
            for cond in (ov.when, ov.arms.when if ov.arms else None)
            if cond is not None and cond.figure
        }
        if missing := referenced - self.figures.keys():
            raise ValueError(f"{self.id} : chiffres référencés mais absents de 'figures' : {sorted(missing)}")
        return self


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


# --------------------------------------------------------------------------- agent


class Source(_Strict):
    name: str
    type: SourceType
    primary: bool
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)   # validés par le fetcher correspondant


class Position(_Strict):
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


class AgentConfig(_Strict):
    agent_id: str = Field(pattern=r"^[A-Z0-9_]+$")
    company: str
    thesis: str
    context_updated_at: date
    position: Position
    sources: list[Source]
    keywords: list[str]
    rules: list[Rule]

    # Contenu de prompt.md : hors du YAML (attribut privé), renseigné par `load_agent`.
    _prompt: str = PrivateAttr(default="")

    @property
    def prompt(self) -> str:
        return self._prompt

    def with_prompt(self, prompt: str) -> AgentConfig:
        self._prompt = prompt
        return self

    @model_validator(mode="after")
    def _consistency(self) -> AgentConfig:
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("IDs de règles dupliqués")
        unknown = {ref for r in self.rules for ref in r.unless_fired} - set(ids)
        if unknown:
            raise ValueError(f"unless_fired référence des règles inconnues : {sorted(unknown)}")
        names = [s.name for s in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("noms de sources dupliqués (le nom sert de clé à l'état des sources)")
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

    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]


# --------------------------------------------------------------------------- paramètres globaux


class ModelsDefaults(_Strict):
    triage: str
    analysis: str


class ScheduleDefaults(_Strict):
    timezone: str
    heartbeat_weekday: int = Field(ge=0, le=6)   # 0 = lundi

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"fuseau horaire inconnu : {value}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class IngestionDefaults(_Strict):
    max_item_age_days: int = Field(gt=0)
    max_event_age_days: int = Field(gt=0)
    max_doc_chars: int = Field(gt=0)
    triage_batch_size: int = Field(gt=0)
    analysis_batch_size: int = Field(default=10, gt=0)   # documents par appel d'analyse (textes complets)


class GuardrailsDefaults(_Strict):
    actionable_actions: list[Action]
    min_confidence_actionable: float = Field(ge=0, le=1)


class RumorDefaults(_Strict):
    severity: Severity
    promote_if_abs_move_pct: float = Field(gt=0)
    promoted_severity: Severity

    @model_validator(mode="after")
    def _never_critical(self) -> RumorDefaults:
        if Severity.CRITICAL in (self.severity, self.promoted_severity):
            raise ValueError("une rumeur n'est jamais CRITICAL")
        return self


class PriceAnomalyDefaults(_Strict):
    rule_id: str
    abs_move_pct: float = Field(gt=0)
    quiet_days: int = Field(ge=0)
    action: Action
    severity: Severity


class OutcomeDefaults(_Strict):
    action: Action
    severity: Severity


class DedupDefaults(_Strict):
    event_window_days: int = Field(gt=0)


class DigestDefaults(_Strict):
    send_if_empty: bool = False


class HeartbeatDefaults(_Strict):
    context_max_age_days: int = Field(gt=0)
    time_stop_warning_days: int = Field(ge=0)
    shares_outstanding_max_age_days: int = Field(gt=0)   # au-delà : shares_outstanding signalé comme ancien


class LlmPrice(_Strict):
    """Tarif d'un modèle en USD par million de tokens (estimation de coût du heartbeat)."""

    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok) / 1_000_000


class LlmBudgetDefaults(_Strict):
    max_total_tokens_per_run: int = Field(gt=0)


class Defaults(_Strict):
    models: ModelsDefaults
    schedule: ScheduleDefaults
    ingestion: IngestionDefaults
    guardrails: GuardrailsDefaults
    rumor: RumorDefaults
    price_anomaly: PriceAnomalyDefaults
    missing_figures: OutcomeDefaults
    priority: list[Action]
    dedup: DedupDefaults
    digest: DigestDefaults
    heartbeat: HeartbeatDefaults
    llm_budget: LlmBudgetDefaults
    llm_pricing: dict[str, LlmPrice] = Field(default_factory=dict)   # clé : `provider:modèle`

    @field_validator("priority")
    @classmethod
    def _complete_priority(cls, value: list[Action]) -> list[Action]:
        if len(value) != len(set(value)) or set(value) != set(Action):
            raise ValueError("priority doit lister chaque action exactement une fois")
        return value


# --------------------------------------------------------------------------- chargement


@dataclass(frozen=True)
class AgentLoadError:
    agent_id: str
    message: str

    @property
    def fingerprint(self) -> str:
        """Empreinte stable du message : sert à ne notifier une erreur de config qu'une fois."""
        return hashlib.sha256(self.message.encode("utf-8")).hexdigest()


@dataclass
class AgentsLoadResult:
    configs: dict[str, AgentConfig] = field(default_factory=dict)
    errors: dict[str, AgentLoadError] = field(default_factory=dict)


SourceValidator = Callable[[Source], None]


def format_validation_error(exc: ValidationError) -> str:
    """Message lisible et stable (sans URL de doc Pydantic, qui change avec la version)."""
    lines = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err["loc"]) or "(racine)"
        lines.append(f"{loc} : {err['msg']}")
    return "\n".join(lines)


def _read_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"fichier absent : {path.name}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path.name} : {exc}") from exc


def _read_prompt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"fichier absent : {path.name} (consignes de l'agent d'analyse)") from exc
    if not text:
        raise ConfigError(f"{path.name} est vide")
    return text


def load_defaults(agents_dir: Path) -> Defaults:
    data = _read_yaml(agents_dir / DEFAULTS_FILE)
    try:
        return Defaults.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{DEFAULTS_FILE} invalide :\n{format_validation_error(exc)}") from exc


def discover_agents(agents_dir: Path) -> dict[str, Path]:
    """Un dossier `agents/<id>/` = un agent. Les noms commençant par `_` ou `.` sont réservés."""
    if not agents_dir.is_dir():
        raise ConfigError(f"dossier des agents introuvable : {agents_dir}")
    return {
        p.name.upper(): p
        for p in sorted(agents_dir.iterdir())
        if p.is_dir() and not p.name.startswith(("_", "."))
    }


def load_agent(agent_dir: Path, validate_source: SourceValidator | None = None) -> AgentConfig:
    expected_id = agent_dir.name.upper()
    data = _read_yaml(agent_dir / AGENT_CONFIG_FILE)
    try:
        cfg = AgentConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from exc
    if cfg.agent_id != expected_id:
        raise ConfigError(f"agent_id {cfg.agent_id!r} différent du nom du dossier (attendu {expected_id!r})")
    cfg.with_prompt(_read_prompt(agent_dir / AGENT_PROMPT_FILE))
    if validate_source is not None:
        for source in cfg.enabled_sources():
            try:
                validate_source(source)
            except ValueError as exc:
                raise ConfigError(f"source {source.name!r} ({source.type}) : {exc}") from exc
    return cfg


def load_agents(
    agents_dir: Path,
    only: str | None = None,
    validate_source: SourceValidator | None = None,
) -> AgentsLoadResult:
    """Charge chaque agent indépendamment : une erreur n'empêche jamais le chargement des autres.

    Lève `ConfigError` seulement si `only` désigne un agent inexistant.
    """
    dirs = discover_agents(agents_dir)
    if only is not None:
        only = only.upper()
        if only not in dirs:
            raise ConfigError(f"agent inconnu : {only} (disponibles : {', '.join(dirs) or 'aucun'})")
        dirs = {only: dirs[only]}

    result = AgentsLoadResult()
    for agent_id, agent_dir in dirs.items():
        try:
            result.configs[agent_id] = load_agent(agent_dir, validate_source)
        except ConfigError as exc:
            result.errors[agent_id] = AgentLoadError(agent_id, str(exc))
            log.error("config %s invalide, agent désactivé pour ce run : %s", agent_id, exc)
        except Exception as exc:  # défense en profondeur : un agent ne doit jamais bloquer les autres
            result.errors[agent_id] = AgentLoadError(agent_id, f"erreur inattendue : {exc!r}")
            log.exception("chargement inattendu en échec pour %s", agent_id)
    return result
