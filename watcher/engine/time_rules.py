"""Règles `time` (cadrage §6.4) : date butoir ou durée de détention maximale.

`unless_fired` est déjà appliqué par `AgentConfig.active_rules`. Une règle échue produit une alerte à chaque run :
c'est le dédoublonnage (clé `(agent, rule_id)`, une seule fois) qui garantit qu'elle n'est jamais répétée.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta

from watcher.config import AgentConfig, Rule, TimeRule
from watcher.models import Alert

log = logging.getLogger(__name__)


def due_date(rule: TimeRule, cfg: AgentConfig) -> date | None:
    if rule.deadline is not None:
        return rule.deadline
    if cfg.position.entry_date is None or rule.max_holding_days is None:
        return None
    return cfg.position.entry_date + timedelta(days=rule.max_holding_days)


def evaluate_time_rules(cfg: AgentConfig, rules: Sequence[Rule], today: date) -> list[Alert]:
    alerts: list[Alert] = []
    for rule in rules:
        if not isinstance(rule, TimeRule):
            continue
        due = due_date(rule, cfg)
        if due is None:
            log.warning("%s : %s non évaluable, date d'entrée absente", cfg.agent_id, rule.id)
            continue
        if today < due:
            continue
        if rule.deadline is not None:
            rationale = f"Date butoir du {due:%d/%m/%Y} atteinte."
        else:
            rationale = (f"Position ouverte le {cfg.position.entry_date:%d/%m/%Y} : durée de détention maximale "
                         f"de {rule.max_holding_days} jours atteinte le {due:%d/%m/%Y}.")
        alerts.append(Alert(
            agent_id=cfg.agent_id,
            rule_id=rule.id,
            source_rule_id=rule.id,
            origin="time",
            action=rule.action,
            severity=rule.severity,
            headline=rule.note or f"{rule.id} : échéance atteinte",
            rationale=rationale,
            event_date=due,
        ))
    return alerts
