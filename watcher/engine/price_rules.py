"""Règles évaluées sur le cours : règles `price`, surveillances armées, anomalie de prix (cadrage §6.2, §6.3, §6.5).

Toutes exigent une nouvelle clôture (`is_new_close`) : le week-end et les jours fériés, rien n'est réévalué.
Règles `price` et surveillances armées ne concernent qu'une position détenue (`OWNED`) ; l'anomalie s'applique
aussi en `WATCH`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from watcher.config import AgentConfig, Condition, Defaults, PriceRule, Rule
from watcher.engine.metrics import MetricContext, MetricUnavailable, compare
from watcher.models import Alert, PriceSnapshot
from watcher.store import ArmedWatch

log = logging.getLogger(__name__)

ARM_REF_FIGURE = "ref_value"   # nom du chiffre de référence d'une surveillance dans `Alert.figures`
_OP_TEXT = {">=": "≥", ">": ">", "<=": "≤", "<": "<"}


def _num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _num_short(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _fresh_close(price: PriceSnapshot | None) -> PriceSnapshot | None:
    return price if price is not None and price.is_new_close else None


# --------------------------------------------------------------------------- règles price


def evaluate_price_rules(
    cfg: AgentConfig,
    rules: Sequence[Rule],
    price: PriceSnapshot | None,
    *,
    watch_active: bool,
) -> list[Alert]:
    """Règles `price` actives. `watch_active` : une surveillance armée est active (neutralise `unless_armed`)."""
    price = _fresh_close(price)
    if price is None or cfg.position.status != "OWNED":
        return []
    alerts: list[Alert] = []
    for rule in rules:
        if not isinstance(rule, PriceRule):
            continue
        if rule.unless_armed and watch_active:
            log.info("%s : %s neutralisée, une surveillance armée est active", cfg.agent_id, rule.id)
            continue
        ctx = MetricContext(cfg.position, price)
        try:
            hit = ctx.check(rule.when)
        except MetricUnavailable as exc:
            log.warning("%s : %s non évaluable : %s", cfg.agent_id, rule.id, exc)
            continue
        if not hit:
            continue
        value = ctx.computed[rule.when.metric]
        alerts.append(Alert(
            agent_id=cfg.agent_id,
            rule_id=rule.id,
            source_rule_id=rule.id,
            origin="price",
            action=rule.action,
            severity=rule.severity,
            headline=rule.note or f"{rule.id} : condition de cours atteinte",
            rationale=(
                f"Clôture du {price.last_close_date:%d/%m/%Y} : {_num(price.last_close)} {cfg.position.currency}. "
                f"{rule.when.metric} = {_num(value, 3)} (seuil {_OP_TEXT[rule.when.op]} {_num_short(rule.when.value)})."
            ),
            event_date=price.last_close_date,
            metrics=ctx.computed,
            checks=ctx.checks,
            price=price,
        ))
    return alerts


# --------------------------------------------------------------------------- surveillances armées


def evaluate_armed_watches(
    cfg: AgentConfig,
    watches: Sequence[ArmedWatch],
    price: PriceSnapshot | None,
) -> list[tuple[ArmedWatch, Alert]]:
    """Surveillances actives dont la condition est vraie sur la dernière clôture."""
    price = _fresh_close(price)
    if price is None or cfg.position.status != "OWNED":
        return []
    fired: list[tuple[ArmedWatch, Alert]] = []
    for watch in watches:
        cond = Condition(metric=watch.metric, op=watch.op, value=watch.threshold, figure=ARM_REF_FIGURE)
        ctx = MetricContext(cfg.position, price, figures={ARM_REF_FIGURE: watch.ref_value})
        try:
            hit = ctx.check(cond)
        except MetricUnavailable as exc:
            log.warning("%s : surveillance %s non évaluable : %s", cfg.agent_id, watch.arm_id, exc)
            continue
        if not hit:
            log.info("%s : surveillance %s active, condition non atteinte", cfg.agent_id, watch.arm_id)
            continue
        ratio = ctx.computed[f"{cond.metric}({ARM_REF_FIGURE})"]
        currency = cfg.position.currency
        fired.append((watch, Alert(
            agent_id=cfg.agent_id,
            rule_id=watch.arm_id,
            source_rule_id=watch.arm_id,
            origin="arm",
            action=watch.action,
            severity=watch.severity,
            headline=(f"Cours à {_num(100 * ratio, 1)} % du prix de référence "
                      f"({_num(watch.ref_value)} {currency})"),
            rationale=(
                f"Surveillance {watch.arm_id} armée le {watch.created_at[:10]} sur un prix de référence de "
                f"{_num(watch.ref_value)} {currency}. Clôture du {price.last_close_date:%d/%m/%Y} : "
                f"{_num(price.last_close)} {currency}, soit {_num(ratio, 3)} × la référence "
                f"(seuil {_OP_TEXT[cond.op]} {_num_short(cond.value)})."
            ),
            event_date=price.last_close_date,
            figures={ARM_REF_FIGURE: watch.ref_value},
            metrics=ctx.computed,
            checks=ctx.checks,
            price=price,
        )))
    return fired


# --------------------------------------------------------------------------- anomalie


def evaluate_anomaly(
    cfg: AgentConfig,
    defaults: Defaults,
    price: PriceSnapshot | None,
    *,
    explained: bool,
) -> Alert | None:
    """`P-ANOMALY` : forte variation J-1 sans actualité. `explained` : match ce run ou événement récent."""
    price = _fresh_close(price)
    anomaly = defaults.price_anomaly
    if price is None or price.daily_move_pct is None:
        return None
    move = price.daily_move_pct
    if not compare(abs(move), ">=", anomaly.abs_move_pct):
        return None
    if explained:
        log.info("%s : variation de %s %% expliquée par une actualité, pas d'anomalie", cfg.agent_id, _num(move, 1))
        return None
    signed = f"{'+' if move > 0 else ''}{_num(move, 1)}"
    return Alert(
        agent_id=cfg.agent_id,
        rule_id=anomaly.rule_id,
        source_rule_id=anomaly.rule_id,
        origin="anomaly",
        action=anomaly.action,
        severity=anomaly.severity,
        headline=f"Mouvement inexpliqué de {signed} %, cherche la source",
        rationale=(
            f"Clôture du {price.last_close_date:%d/%m/%Y} : {_num(price.last_close)} {cfg.position.currency} "
            f"({signed} % sur la séance), sans actualité détectée ce run ni événement depuis "
            f"{defaults.price_anomaly.quiet_days} jours."
        ),
        event_date=price.last_close_date,
        metrics={"daily_move_pct": round(move, 6)},
        price=price,
    )
