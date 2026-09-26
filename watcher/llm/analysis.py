"""Agent d'analyse (cadrage §7.2) : identifie les événements (`rule_id`) et extrait les chiffres bruts.

L'`output_validator` n'exige que des références valides (`rule_id` actif, documents fournis). Il ne vérifie
**pas** `extracted_figures` : forcer un retry sur un chiffre absent pousserait le modèle à en inventer un.
Un chiffre manquant est géré par le moteur (`missing_figures` → RECO_UNCLEAR).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from watcher.config import AgentConfig, EventRule
from watcher.llm.instructions import analysis_instructions, analysis_prompt
from watcher.llm.runtime import TokenBudget, assign_refs, batched, unknown_refs
from watcher.models import Analysis, NewsItem, PriceSnapshot, RuleMatch

log = logging.getLogger(__name__)

# Sonnet 5 réfléchit en mode adaptatif par défaut : la réflexion compte dans max_tokens.
ANALYSIS_MAX_TOKENS = 16000


@dataclass
class AnalysisDeps:
    cfg: AgentConfig
    rules: list[EventRule]          # règles event actives, déjà filtrées
    items: dict[str, NewsItem]      # documents fournis, indexés par référence courte (D1, D2...)


analyst: Agent[AnalysisDeps, Analysis] = Agent(
    deps_type=AnalysisDeps, output_type=Analysis, retries=2, name="analysis"
)


@analyst.output_validator
def check_references(ctx: RunContext[AnalysisDeps], out: Analysis) -> Analysis:
    valid_rules = {r.id for r in ctx.deps.rules}
    for m in out.matches:
        if m.rule_id not in valid_rules:
            raise ModelRetry(f"rule_id inconnu : {m.rule_id}. IDs valides : {sorted(valid_rules)}")
        if unknown := unknown_refs(m.item_ids, ctx.deps.items):
            raise ModelRetry(f"item_ids inconnus : {unknown}. Références valides : {list(ctx.deps.items)}")
    return out


def to_real_ids(match: RuleMatch, refs: dict[str, NewsItem], rule: EventRule) -> RuleMatch:
    """Références courtes → IDs réels ; seuls les chiffres déclarés par la règle sont conservés."""
    item_ids = list(dict.fromkeys(refs[r].id for r in match.item_ids))
    figures = {k: v for k, v in match.extracted_figures.items() if k in rule.figures}
    if dropped := sorted(match.extracted_figures.keys() - figures.keys()):
        log.warning("%s : chiffres non demandés par la règle ignorés : %s", match.rule_id, dropped)
    return match.model_copy(update={"item_ids": item_ids, "extracted_figures": figures})


def run_analysis(
    items: Sequence[NewsItem],
    cfg: AgentConfig,
    rules: Sequence[EventRule],
    price: PriceSnapshot | None,
    *,
    today: date,
    model: Model,
    budget: TokenBudget,
    batch_size: int,
    max_doc_chars: int,
) -> list[RuleMatch]:
    """Matches de tous les lots, avec les IDs réels des documents. Lève `LlmError` si un lot échoue."""
    rules = list(rules)
    by_id = {r.id: r for r in rules}
    instructions = analysis_instructions(cfg, rules)
    matches: list[RuleMatch] = []
    batches = list(batched(items, batch_size))
    for n, batch in enumerate(batches, start=1):
        refs = assign_refs(batch)
        label = f"{cfg.agent_id} : analyse, lot {n}/{len(batches)}"
        with budget.call(label) as (usage, limits):
            result = analyst.run_sync(
                analysis_prompt(cfg, refs, price, today=today, max_doc_chars=max_doc_chars),
                model=model, instructions=instructions, deps=AnalysisDeps(cfg, rules, refs),
                usage=usage, usage_limits=limits,
            )
        found = [to_real_ids(m, refs, by_id[m.rule_id]) for m in result.output.matches]
        for m in found:
            log.info("%s : match %s (confiance %.2f, date %s, %d document(s), chiffres %s) : %s", label,
                     m.rule_id, m.confidence, m.event_date.isoformat(), len(m.item_ids),
                     m.extracted_figures or "aucun", m.headline)
        if not found:
            log.info("%s : aucun match", label)
        matches += found
    return matches
