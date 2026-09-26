"""Notation des evals sur fixtures (cadrage §12.2). Aucun appel LLM ici : la notation est testée à part.

Critères d'acceptation :
- zéro faux négatif sur les cas qui mènent à RECO_SELL_ALL avec source primaire ;
- zéro faux positif de vente sur les cas qui n'attendent pas la règle concernée ;
- chiffres extraits exacts sur tous les cas qui en attendent.

Une règle « de vente » est une règle `event` dont l'action par défaut, un override ou une surveillance armée
recommande de vendre : un faux match sur elle peut, selon les métriques, finir en recommandation de vente.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from watcher.config import Action, AgentConfig, EventRule
from watcher.fixtures import Fixture
from watcher.models import RuleMatch

SALE_ACTIONS = frozenset({Action.RECO_SELL_ALL, Action.RECO_SELL_HALF})
FIGURE_REL_TOLERANCE = 1e-9


def rule_actions(rule: EventRule) -> set[Action]:
    actions = {rule.action}
    for ov in rule.overrides:
        actions.add(ov.action)
        if ov.arms is not None:
            actions.add(ov.arms.action)
    return actions


def sale_rule_ids(rules: Iterable[EventRule]) -> set[str]:
    return {r.id for r in rules if rule_actions(r) & SALE_ACTIONS}


def sell_all_rule_ids(rules: Iterable[EventRule]) -> set[str]:
    return {r.id for r in rules if Action.RECO_SELL_ALL in rule_actions(r)}


def eval_config(cfg: AgentConfig, fixture: Fixture) -> AgentConfig:
    """Config de l'agent, avec la phase simulée par la fixture (`status`) le cas échéant."""
    status = fixture.header.status
    if status is None or status == cfg.position.status:
        return cfg
    position = cfg.position.model_copy(update={"status": status})
    return cfg.model_copy(update={"position": position}).with_prompt(cfg.prompt)


@dataclass
class CaseResult:
    fixture: str
    model: str
    synthetic: bool
    expected: list[str]
    found: list[str]
    critical: bool                                              # cas SELL_ALL avec source primaire
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    sale_false_positives: list[str] = field(default_factory=list)
    figure_errors: list[str] = field(default_factory=list)
    error: str | None = None                                    # échec de la couche LLM

    @property
    def critical_false_negative(self) -> bool:
        return self.critical and bool(self.missing)

    @property
    def passed(self) -> bool:
        """Critères d'acceptation du cadrage (les autres écarts sont seulement rapportés)."""
        return (self.error is None and not self.critical_false_negative and not self.sale_false_positives
                and not self.figure_errors)

    @property
    def exact(self) -> bool:
        return self.passed and not self.missing and not self.unexpected


def score_case(fixture: Fixture, cfg: AgentConfig, matches: Sequence[RuleMatch], *, model: str) -> CaseResult:
    h = fixture.header
    rules = cfg.event_rules(set())
    expected = set(h.expected_rule_ids)
    tolerated = expected | set(h.allowed_rule_ids)
    found = [m.rule_id for m in matches]
    result = CaseResult(
        fixture=fixture.name, model=model, synthetic=h.synthetic, expected=sorted(expected),
        found=sorted(set(found)), critical=h.source_primary and bool(expected & sell_all_rule_ids(rules)),
        missing=sorted(expected - set(found)), unexpected=sorted(set(found) - tolerated),
    )
    result.sale_false_positives = sorted(set(result.unexpected) & sale_rule_ids(rules))
    by_rule = {m.rule_id: m for m in matches if m.rule_id in expected}
    for name, value in h.expected_figures.items():
        owners = [m for m in by_rule.values() if name in m.extracted_figures]
        if not by_rule:
            continue                                    # règle manquée : déjà comptée dans `missing`
        if not owners:
            result.figure_errors.append(f"{name} absent (attendu {value:g})")
        elif not math.isclose(got := owners[0].extracted_figures[name], value, rel_tol=FIGURE_REL_TOLERANCE):
            result.figure_errors.append(f"{name} = {got:g} (attendu {value:g})")
    return result


def format_report(results: Sequence[CaseResult]) -> str:
    """Tableau récapitulatif par cas et par modèle (Markdown)."""
    lines = [
        "| Modèle | Cas | Synth. | Attendu | Trouvé | Critères | Détail |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (r.model, r.fixture)):
        details = []
        if r.error:
            details.append(f"erreur : {r.error}")
        if r.critical_false_negative:
            details.append("FAUX NÉGATIF CRITIQUE")
        if r.sale_false_positives:
            details.append(f"faux positif de vente : {', '.join(r.sale_false_positives)}")
        details += r.figure_errors
        if r.missing and not r.critical_false_negative:
            details.append(f"manqué : {', '.join(r.missing)}")
        if r.unexpected and not r.sale_false_positives:
            details.append(f"en trop : {', '.join(r.unexpected)}")
        status = "OK" if r.exact else ("OK (écarts)" if r.passed else "ÉCHEC")
        lines.append(f"| {r.model} | {r.fixture} | {'oui' if r.synthetic else 'non'} | "
                     f"{', '.join(r.expected) or '—'} | {', '.join(r.found) or '—'} | {status} | "
                     f"{' ; '.join(details)} |")
    passed = sum(r.passed for r in results)
    exact = sum(r.exact for r in results)
    lines.append("")
    lines.append(f"{passed}/{len(results)} cas conformes aux critères d'acceptation, {exact} sans aucun écart.")
    return "\n".join(lines)
