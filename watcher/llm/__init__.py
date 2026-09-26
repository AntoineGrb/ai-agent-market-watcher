"""Couche LLM d'un agent (étapes 5 et 6 du run, cadrage §3.1 et §7).

Tri (Haiku) des nouveaux documents → texte complet des seuls documents retenus → analyse (Sonnet).
Le LLM ne produit que des `RuleMatch` ; tout le reste (action, sévérité, seuils) est décidé par le moteur.

Toute erreur LLM (retries épuisés, API indisponible, budget épuisé) lève `LlmError` : l'agent est en échec pour
ce run et ses documents ne sont pas marqués vus, ils seront retraités au run suivant.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from pydantic_ai.models import Model

from watcher.config import AgentConfig, Defaults
from watcher.llm.analysis import ANALYSIS_MAX_TOKENS, run_analysis
from watcher.llm.runtime import (
    LlmBudgetExceeded,
    LlmConfigError,
    LlmError,
    LlmUsage,
    TokenBudget,
    resolve_model,
)
from watcher.llm.triage import TRIAGE_MAX_TOKENS, run_triage
from watcher.models import NewsItem, PriceSnapshot, RuleMatch
from watcher.settings import Settings
from watcher.sources.base import get_fetcher

__all__ = [
    "LlmBudgetExceeded", "LlmConfigError", "LlmError", "LlmLayer", "LlmOutcome", "LlmUsage", "TokenBudget",
]

log = logging.getLogger(__name__)

TextFetcher = Callable[[NewsItem], str]


@dataclass
class LlmOutcome:
    matches: list[RuleMatch] = field(default_factory=list)
    items: dict[str, NewsItem] = field(default_factory=dict)   # documents retenus (avec texte), par ID réel
    relevant: set[str] = field(default_factory=set)            # IDs retenus au tri (seen_items.triage_relevant)
    warnings: list[str] = field(default_factory=list)          # textes complets indisponibles


def fetch_text_from_source(item: NewsItem) -> str:
    """Texte complet via le fetcher du type de source du document."""
    fetcher = get_fetcher(item.source_type)
    if fetcher is None:
        raise LookupError(f"aucun fetcher pour le type {item.source_type}")
    return fetcher.fetch_text(item)


class LlmLayer:
    """Tri et analyse d'un agent. Les modèles sont résolus au premier appel, pas à la construction."""

    def __init__(
        self,
        settings: Settings,
        *,
        triage_model: Model | None = None,      # surcharges (tests) : sinon config / variables d'environnement
        analysis_model: Model | None = None,
        text_fetcher: TextFetcher = fetch_text_from_source,
    ) -> None:
        self._settings = settings
        self._overrides = {"triage": triage_model, "analysis": analysis_model}
        self._text_fetcher = text_fetcher
        self._models: dict[str, Model] = {}

    def model_names(self, defaults: Defaults) -> dict[str, str]:
        return {
            "triage": self._settings.model_triage or defaults.models.triage,
            "analysis": self._settings.model_analysis or defaults.models.analysis,
        }

    def _model(self, role: str, defaults: Defaults) -> Model:
        if (override := self._overrides[role]) is not None:
            return override
        name = self.model_names(defaults)[role]
        if name not in self._models:
            max_tokens = TRIAGE_MAX_TOKENS if role == "triage" else ANALYSIS_MAX_TOKENS
            self._models[name] = resolve_model(name, self._settings, max_tokens=max_tokens)
            log.info("modèle de %s : %s", "tri" if role == "triage" else "l'analyse", name)
        return self._models[name]

    def process(
        self,
        cfg: AgentConfig,
        defaults: Defaults,
        items: Sequence[NewsItem],
        price: PriceSnapshot | None,
        *,
        today: date,
        fired: set[str],
        budget: TokenBudget,
    ) -> LlmOutcome:
        outcome = LlmOutcome()
        rules = cfg.event_rules(fired)
        if not items:
            return outcome
        if not rules:
            log.info("%s : aucune règle event active, %d document(s) ni triés ni analysés", cfg.agent_id, len(items))
            return outcome
        ingestion = defaults.ingestion

        outcome.relevant = run_triage(items, cfg, rules, model=self._model("triage", defaults), budget=budget,
                                      batch_size=ingestion.triage_batch_size)
        log.info("%s : tri : %d document(s) retenu(s) sur %d", cfg.agent_id, len(outcome.relevant), len(items))
        retained = [self._with_text(it, cfg, outcome) for it in items if it.id in outcome.relevant]
        if not retained:
            return outcome

        outcome.items = {it.id: it for it in retained}
        outcome.matches = run_analysis(
            retained, cfg, rules, price, today=today, model=self._model("analysis", defaults), budget=budget,
            batch_size=ingestion.analysis_batch_size, max_doc_chars=ingestion.max_doc_chars,
        )
        log.info("%s : analyse : %d match(es) sur %d document(s)", cfg.agent_id, len(outcome.matches), len(retained))
        return outcome

    def _with_text(self, item: NewsItem, cfg: AgentConfig, outcome: LlmOutcome) -> NewsItem:
        """Texte complet d'un document retenu. En cas d'échec : titre et résumé, avec un avertissement."""
        if item.text:
            return item
        try:
            text = self._text_fetcher(item)
        except Exception as exc:   # une source en erreur ne fait jamais échouer l'agent (cadrage §8.1)
            log.warning("%s : texte de %s indisponible (%s : %s), analyse sur le titre et le résumé",
                        cfg.agent_id, item.url, type(exc).__name__, exc)
            outcome.warnings.append(f"texte indisponible ({item.source_name}) : {type(exc).__name__} : {exc}")
            text = f"{item.title}\n{item.summary}\n(texte complet indisponible)"
        return item.model_copy(update={"text": text})
