"""Dédoublonnage des alertes, niveau 2 (cadrage §6.7).

| origine  | clé                             | règle                                                      |
|----------|---------------------------------|------------------------------------------------------------|
| event    | (agent, rule_id affiché)        | supprimée si même clé depuis < event_window_days, sauf escalade |
| price    | (agent, rule_id, entry_date)    | une fois par position                                      |
| arm/time | (agent, rule_id)                | une fois                                                   |
| anomaly  | (agent, date de clôture)        | une fois par clôture                                       |

L'agent n'entre pas dans la clé : elle est toujours interrogée avec l'`agent_id` (colonne dédiée de `events`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from watcher.config import Action
from watcher.engine.priority import action_rank, severity_rank
from watcher.models import Alert


def dedup_key(alert: Alert, entry_date: date | None) -> str:
    match alert.origin:
        case "event":
            return f"event:{alert.rule_id}"
        case "price":
            return f"price:{alert.rule_id}:{entry_date.isoformat() if entry_date else '-'}"
        case "arm" | "time":
            return f"{alert.origin}:{alert.rule_id}"
        case "anomaly":
            return f"anomaly:{alert.event_date.isoformat()}"
    raise ValueError(f"origine inconnue : {alert.origin}")   # pragma: no cover - Literal exhaustif


def uses_window(alert: Alert) -> bool:
    """Seuls les événements LLM sont dédoublonnés sur une fenêtre glissante ; les autres le sont à vie."""
    return alert.origin == "event"


def escalation(new: Alert, old: Alert, priority: Sequence[Action]) -> str | None:
    """Raison de l'escalade de `new` par rapport à `old`, ou None."""
    if old.is_rumor and not new.is_rumor:
        return "rumeur confirmée par une source primaire"
    if severity_rank(new.severity) < severity_rank(old.severity):
        return f"sévérité {old.severity.value} → {new.severity.value}"
    if action_rank(new.action, priority) < action_rank(old.action, priority):
        return f"action {old.action.value} → {new.action.value}"
    return None


def should_emit(alert: Alert, previous: Sequence[Alert], priority: Sequence[Action]) -> tuple[bool, str]:
    """Décide si l'alerte part, face aux alertes de même clé déjà enregistrées (déjà filtrées sur la fenêtre).

    Pour un événement, il faut une escalade par rapport à **chacune** des alertes précédentes : une rumeur qui
    suit une confirmation primaire reste supprimée même si elle escalade par rapport à une rumeur plus ancienne.
    """
    if not previous:
        return True, "nouvelle alerte"
    if not uses_window(alert):
        return False, "déjà émise"
    reasons = [escalation(alert, old, priority) for old in previous]
    if all(reasons):
        return True, f"escalade : {reasons[-1]}"
    return False, "déjà émise dans la fenêtre de dédoublonnage, sans escalade"
