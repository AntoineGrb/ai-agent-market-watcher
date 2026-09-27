"""Agent de tri (cadrage §7.1) : réduit le bruit avant l'analyse, en privilégiant le rappel.

Entrée : titre, source, date et résumé tronqué de chaque document, par lots de `triage_batch_size`.
Sortie : `TriageResult`, dont les références sont vérifiées par l'`output_validator` (sinon `ModelRetry`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from watcher.config import AgentConfig, EventRule
from watcher.llm.instructions import triage_instructions, triage_prompt
from watcher.llm.runtime import TokenBudget, assign_refs, batched, model_key, unknown_refs
from watcher.models import NewsItem, TriageResult

log = logging.getLogger(__name__)

TRIAGE_MAX_TOKENS = 2048   # une liste de références courtes


@dataclass
class TriageDeps:
    refs: dict[str, NewsItem]   # documents du lot, indexés par référence courte


triage_agent: Agent[TriageDeps, TriageResult] = Agent(
    deps_type=TriageDeps, output_type=TriageResult, retries=2, name="triage"
)


@triage_agent.output_validator
def check_triage_refs(ctx: RunContext[TriageDeps], out: TriageResult) -> TriageResult:
    if unknown := unknown_refs(out.relevant_ids, ctx.deps.refs):
        raise ModelRetry(f"références inconnues : {unknown}. Références valides : {list(ctx.deps.refs)}")
    return out


def run_triage(
    items: Sequence[NewsItem],
    cfg: AgentConfig,
    rules: Sequence[EventRule],
    *,
    model: Model,
    budget: TokenBudget,
    batch_size: int,
) -> set[str]:
    """IDs (réels) des documents retenus. Lève `LlmError` si un lot échoue après les retries."""
    instructions = triage_instructions(cfg, rules)
    relevant: set[str] = set()
    batches = list(batched(items, batch_size))
    for n, batch in enumerate(batches, start=1):
        refs = assign_refs(batch)
        label = f"{cfg.agent_id} : tri, lot {n}/{len(batches)}"
        with budget.call(label, model_key(model)) as (usage, limits):
            result = triage_agent.run_sync(
                triage_prompt(refs), model=model, instructions=instructions, deps=TriageDeps(refs),
                usage=usage, usage_limits=limits,
            )
        kept = {refs[r].id for r in result.output.relevant_ids}
        log.info("%s : %d document(s) retenu(s) sur %d", label, len(kept), len(batch))
        relevant |= kept
    return relevant
