"""Priorité des alertes (cadrage §6.6) : sévérité, puis action selon `_defaults.yaml` → `priority`."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from watcher.config import Action, Severity
from watcher.models import Alert

SEVERITY_ORDER: tuple[Severity, ...] = (Severity.CRITICAL, Severity.HIGH, Severity.INFO)


def severity_rank(severity: Severity) -> int:
    """0 = la plus grave."""
    return SEVERITY_ORDER.index(severity)


def action_rank(action: Action, priority: Sequence[Action]) -> int:
    """0 = la plus prioritaire."""
    return list(priority).index(action)


def sort_key(alert: Alert, priority: Sequence[Action]) -> tuple[int, int, bool, float, str]:
    # À égalité : source primaire avant rumeur, puis confiance décroissante, puis ID pour un ordre stable.
    confidence = alert.confidence if alert.confidence is not None else 1.0
    return (severity_rank(alert.severity), action_rank(alert.action, priority), alert.is_rumor,
            -confidence, alert.rule_id)


def sort_alerts(alerts: Iterable[Alert], priority: Sequence[Action]) -> list[Alert]:
    return sorted(alerts, key=lambda a: sort_key(a, priority))


def lead_alert(alerts: Iterable[Alert], priority: Sequence[Action]) -> Alert | None:
    """Alerte de tête : c'est elle qui donne l'objet du mail d'un agent."""
    ordered = sort_alerts(alerts, priority)
    return ordered[0] if ordered else None
