"""Résolution des `RuleMatch` du LLM en `Alert` (cadrage §6.1).

Ordre des étapes : règle active → fraîcheur → preuves → overrides → garde-fou actionnable → rumeur → armement,
puis contradictions une fois tous les matches du run résolus. L'action et la sévérité viennent exclusivement de
la config ; le LLM n'apporte que l'identification de la règle, les preuves et les chiffres bruts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from watcher.config import (
    Action,
    AgentConfig,
    Arm,
    Defaults,
    EventRule,
    GuardrailsDefaults,
    OutcomeDefaults,
    Override,
    RumorDefaults,
    Severity,
)
from watcher.engine.metrics import MetricContext, MetricUnavailable, compare
from watcher.models import Alert, Evidence, NewsItem, PriceSnapshot, RuleMatch

log = logging.getLogger(__name__)

REASON_SEPARATOR = " ; "
CONTRADICTION_REASON = "signaux contradictoires sur le même document"
OPPOSITE_DIRECTIONS = {"bullish", "bearish"}


# --------------------------------------------------------------------------- choix de l'issue (overrides)


@dataclass(frozen=True)
class Outcome:
    rule_id: str                  # ID affiché (ID de l'override s'il y en a un)
    action: Action
    severity: Severity
    arm: Arm | None = None
    reason: str | None = None     # renseigné pour missing_figures

    @classmethod
    def default(cls, rule: EventRule) -> Outcome:
        return cls(rule.id, rule.action, rule.severity)

    @classmethod
    def from_override(cls, rule: EventRule, ov: Override) -> Outcome:
        return cls(ov.id or rule.id, ov.action, ov.severity, arm=ov.arms)

    @classmethod
    def missing_figures(cls, rule: EventRule, outcome: OutcomeDefaults, reasons: Sequence[str]) -> Outcome:
        return cls(rule.id, outcome.action, outcome.severity,
                   reason="chiffres non extraits, lis la source : " + REASON_SEPARATOR.join(reasons))


def pick_outcome(rule: EventRule, ctx: MetricContext, missing: OutcomeDefaults) -> Outcome:
    """Le premier override vrai l'emporte ; un override `unclear` non évaluable mène à `missing_figures`."""
    not_evaluable: list[str] = []
    for ov in rule.overrides:
        try:
            if ctx.check(ov.when):
                return Outcome.from_override(rule, ov)
        except MetricUnavailable as exc:
            if ov.if_unavailable == "unclear":
                not_evaluable.append(str(exc))
    if not_evaluable:
        return Outcome.missing_figures(rule, missing, not_evaluable)
    return Outcome.default(rule)


# --------------------------------------------------------------------------- résolution d'un match


@dataclass(frozen=True)
class PendingArm:
    """Surveillance à créer si l'alerte qui l'arme est effectivement écrite dans l'outbox."""

    arm: Arm
    ref_value: float


@dataclass(frozen=True)
class ResolvedMatch:
    alert: Alert
    direction: str
    arm: PendingArm | None = None


def _join(reasons: Iterable[str | None]) -> str | None:
    kept = [r for r in reasons if r]
    return REASON_SEPARATOR.join(kept) if kept else None


def _evidence(match: RuleMatch, items: Mapping[str, NewsItem]) -> list[Evidence]:
    evidence: list[Evidence] = []
    for item_id in dict.fromkeys(match.item_ids):   # dédoublonné, ordre conservé
        item = items.get(item_id)
        if item is None:   # défense en profondeur : le validateur de l'agent d'analyse l'a déjà refusé
            log.warning("match %s : document %s inconnu, preuve ignorée", match.rule_id, item_id)
            continue
        evidence.append(Evidence(item_id=item.id, url=item.url, source_name=item.source_name,
                                 primary=item.source_primary))
    return evidence


def _guardrail_blocker(is_rumor: bool, confidence: float, guardrails: GuardrailsDefaults) -> str | None:
    if is_rumor:
        return "aucune source primaire (rumeur)"
    minimum = guardrails.min_confidence_actionable
    if confidence < minimum:
        return f"confiance {confidence:.2f} < {minimum:.2f}".replace(".", ",")
    return None


def _rumor_severity(rumor: RumorDefaults, price: PriceSnapshot | None) -> Severity:
    move = price.daily_move_pct if price is not None else None
    if move is not None and compare(abs(move), ">=", rumor.promote_if_abs_move_pct):
        return rumor.promoted_severity
    return rumor.severity


def _pending_arm(
    arm: Arm,
    match: RuleMatch,
    *,
    is_rumor: bool,
    downgraded: bool,
    guardrails: GuardrailsDefaults,
) -> tuple[PendingArm | None, str | None]:
    """Retourne la surveillance à créer, ou la raison pour laquelle elle ne l'est pas."""
    if is_rumor:
        blocker: str | None = "aucune source primaire (rumeur)"
    elif downgraded:
        blocker = "recommandation rétrogradée"
    elif arm.action in guardrails.actionable_actions:
        # La surveillance produira plus tard une action actionnable : même exigence de confiance qu'au §6.1-5.
        blocker = _guardrail_blocker(False, match.confidence, guardrails)
    else:
        blocker = None
    if blocker is None:
        figure = arm.when.figure or ""
        if figure not in match.extracted_figures:
            blocker = f"chiffre {figure} non extrait"
        elif match.extracted_figures[figure] <= 0:
            blocker = f"chiffre {figure} invalide ({match.extracted_figures[figure]:g})"
        else:
            return PendingArm(arm, float(match.extracted_figures[figure])), None
    return None, f"surveillance {arm.id} non armée : {blocker}"


def resolve_match(
    match: RuleMatch,
    rules: Mapping[str, EventRule],
    *,
    cfg: AgentConfig,
    defaults: Defaults,
    items: Mapping[str, NewsItem],
    price: PriceSnapshot | None,
    today: date,
) -> ResolvedMatch | None:
    """Résout un match isolé (étapes 1 à 7 du §6.1). Retourne None si le match est ignoré."""
    agent_id = cfg.agent_id

    rule = rules.get(match.rule_id)
    if rule is None:
        log.warning("%s : match sur la règle %s, inactive ou inconnue : ignoré", agent_id, match.rule_id)
        return None

    oldest = today - timedelta(days=defaults.ingestion.max_event_age_days)
    if match.event_date < oldest:
        log.info("%s : match %s daté du %s, antérieur au %s : ignoré (événement ancien)",
                 agent_id, match.rule_id, match.event_date.isoformat(), oldest.isoformat())
        return None

    evidence = _evidence(match, items)
    if not evidence:
        log.warning("%s : match %s sans preuve exploitable : ignoré", agent_id, match.rule_id)
        return None
    is_rumor = not any(e.primary for e in evidence)

    ctx = MetricContext(cfg.position, price, match.extracted_figures, match.event_date)
    outcome = pick_outcome(rule, ctx, defaults.missing_figures)
    action, severity = outcome.action, outcome.severity
    reasons: list[str | None] = [outcome.reason]

    guardrails = defaults.guardrails
    if action in guardrails.actionable_actions:
        blocker = _guardrail_blocker(is_rumor, match.confidence, guardrails)
        if blocker is not None:
            reasons.append(f"{action.value} rétrogradé en {Action.RECO_UNCLEAR.value} : {blocker}")
            action = Action.RECO_UNCLEAR

    if is_rumor:
        severity = _rumor_severity(defaults.rumor, price)

    pending: PendingArm | None = None
    if outcome.arm is not None:
        pending, arm_reason = _pending_arm(outcome.arm, match, is_rumor=is_rumor,
                                           downgraded=action != outcome.action, guardrails=guardrails)
        reasons.append(arm_reason)

    alert = Alert(
        agent_id=agent_id,
        rule_id=outcome.rule_id,
        source_rule_id=rule.id,
        origin="event",
        action=action,
        severity=severity,
        headline=match.headline,
        rationale=match.rationale,
        evidence=evidence,
        is_rumor=is_rumor,
        confidence=match.confidence,
        event_date=match.event_date,
        figures=dict(match.extracted_figures),
        metrics=ctx.computed,
        price=price,
        downgrade_reason=_join(reasons),
    )
    log.info("%s : match %s → %s %s %s%s", agent_id, rule.id, alert.rule_id, alert.action.value,
             alert.severity.value, " (rumeur)" if is_rumor else "")
    return ResolvedMatch(alert, rule.direction, pending)


# --------------------------------------------------------------------------- contradictions


def apply_contradictions(resolved: Sequence[ResolvedMatch]) -> list[ResolvedMatch]:
    """Deux matches de directions opposées partageant un document deviennent tous deux RECO_UNCLEAR (§6.1-8)."""
    evidence_ids = [{e.item_id for e in r.alert.evidence} for r in resolved]
    opponents: dict[int, set[str]] = {}
    for i, a in enumerate(resolved):
        for j in range(i + 1, len(resolved)):
            b = resolved[j]
            if {a.direction, b.direction} == OPPOSITE_DIRECTIONS and evidence_ids[i] & evidence_ids[j]:
                opponents.setdefault(i, set()).add(b.alert.rule_id)
                opponents.setdefault(j, set()).add(a.alert.rule_id)

    out: list[ResolvedMatch] = []
    for i, r in enumerate(resolved):
        if i not in opponents:
            out.append(r)
            continue
        others = ", ".join(sorted(opponents[i]))
        reason = f"rétrogradé en {Action.RECO_UNCLEAR.value} : {CONTRADICTION_REASON} (avec {others})"
        arm_reason = f"surveillance {r.arm.arm.id} non armée : {CONTRADICTION_REASON}" if r.arm else None
        alert = r.alert.model_copy(update={
            "action": Action.RECO_UNCLEAR,
            "downgrade_reason": _join([r.alert.downgrade_reason, reason, arm_reason]),
        })
        log.info("%s : %s rétrogradé, %s", alert.agent_id, alert.rule_id, reason)
        out.append(replace(r, alert=alert, arm=None))
    return out


def resolve_matches(
    matches: Sequence[RuleMatch],
    *,
    cfg: AgentConfig,
    defaults: Defaults,
    items: Mapping[str, NewsItem],
    price: PriceSnapshot | None,
    today: date,
    fired: set[str],
) -> list[ResolvedMatch]:
    """Résout tous les matches d'un run pour un agent, contradictions comprises."""
    rules = {r.id: r for r in cfg.event_rules(fired)}
    resolved = [
        r for m in matches
        if (r := resolve_match(m, rules, cfg=cfg, defaults=defaults, items=items, price=price, today=today))
        is not None
    ]
    return apply_contradictions(resolved)
