"""Textes lisibles des règles et des conditions (mail d'alerte, cadrage §9.1).

`rule_text` est calculé à la création de l'alerte et stocké avec elle : un mail renvoyé depuis l'outbox affiche la
règle telle qu'elle était quand l'alerte a été produite.
"""

from __future__ import annotations

import re

from watcher.config import AgentConfig, Condition, Defaults, EventRule, PriceRule, TimeRule
from watcher.engine.metrics import metric_label
from watcher.engine.price_rules import ARM_REF_FIGURE
from watcher.models import Alert

OP_TEXT = {">=": "≥", ">": ">", "<=": "≤", "<": "<"}

_LABEL = re.compile(r"^(?P<metric>\w+)(?:\((?P<figure>\w+)\))?$")


def num(value: float, digits: int | None = None) -> str:
    """Nombre à la française. `digits=None` : représentation courte (`0,98`, `20`, `-500`)."""
    text = f"{value:g}" if digits is None else f"{value:.{digits}f}"
    return text.replace(".", ",")


def metric_text(label: str) -> str:
    """`figure_vs_prev_close(offer_price)` → `offer_price / clôture précédant l'événement`."""
    m = _LABEL.match(label)
    if m is None:
        return label
    figure = m["figure"]
    if figure == ARM_REF_FIGURE:
        figure = "prix de référence"
    match m["metric"]:
        case "price_vs_entry":
            return "cours / prix d'entrée"
        case "dilution_pct":
            return "dilution (%)"
        case "figure_vs_prev_close":
            return f"{figure} / clôture précédant l'événement"
        case "price_vs_figure":
            return f"cours / {figure}"
        case "figure":
            return figure or label
        case "daily_move_pct":
            return "variation J-1 (%)"
    return label


def condition_text(cond: Condition) -> str:
    return f"{metric_text(metric_label(cond))} {OP_TEXT[cond.op]} {num(cond.value)}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def rule_text(alert: Alert, cfg: AgentConfig, defaults: Defaults) -> str | None:
    """Déclencheur d'une règle `event`, ou condition d'une règle déterministe. None si la règle est introuvable."""
    if alert.origin == "anomaly":
        anomaly = defaults.price_anomaly
        return (f"|variation J-1| ≥ {num(anomaly.abs_move_pct)} % sans actualité associée "
                f"(ni match ce run, ni événement depuis {anomaly.quiet_days} jours)")
    rules = {r.id: r for r in cfg.rules}
    if alert.origin == "arm":
        for rule in rules.values():
            if not isinstance(rule, EventRule):
                continue
            for ov in rule.overrides:
                if ov.arms is not None and ov.arms.id == alert.rule_id:
                    return f"surveillance armée par {ov.id or rule.id} : {condition_text(ov.arms.when)}"
        return None
    rule = rules.get(alert.source_rule_id)
    match rule:
        case EventRule():
            return _one_line(rule.trigger)
        case PriceRule():
            text = condition_text(rule.when)
            return f"{text}, sans surveillance armée active" if rule.unless_armed else text
        case TimeRule(deadline=deadline) if deadline is not None:
            return f"date butoir : {deadline:%d/%m/%Y}"
        case TimeRule():
            return f"durée de détention maximale : {rule.max_holding_days} jours"
    return None
