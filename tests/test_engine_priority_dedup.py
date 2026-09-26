from __future__ import annotations

from datetime import date

import pytest

from watcher.config import Action, Defaults, Severity
from watcher.engine.dedup import dedup_key, escalation, should_emit, uses_window
from watcher.engine.priority import lead_alert, sort_alerts
from watcher.models import Alert

A, S = Action, Severity


def _alert(rule_id: str = "U-S1", action: Action = A.RECO_HOLD, severity: Severity = S.INFO, *,
           origin: str = "event", rumor: bool = False, confidence: float | None = 0.9,
           event_date: date = date(2026, 9, 24)) -> Alert:
    return Alert(agent_id="UBI", rule_id=rule_id, source_rule_id=rule_id, origin=origin, action=action,
                 severity=severity, headline="h", rationale="r", is_rumor=rumor, confidence=confidence,
                 event_date=event_date)


# --------------------------------------------------------------------------- priorité


def test_sort_by_severity_then_action(defaults: Defaults) -> None:
    alerts = [
        _alert("hold-info", A.RECO_HOLD, S.INFO),
        _alert("unclear-high", A.RECO_UNCLEAR, S.HIGH),
        _alert("hold-critical", A.RECO_HOLD, S.CRITICAL),
        _alert("sell-half-high", A.RECO_SELL_HALF, S.HIGH),
        _alert("sell-all-critical", A.RECO_SELL_ALL, S.CRITICAL),
        _alert("buy-high", A.RECO_BUY, S.HIGH),
        _alert("no-entry-high", A.RECO_NO_ENTRY, S.HIGH),
    ]
    assert [a.rule_id for a in sort_alerts(alerts, defaults.priority)] == [
        "sell-all-critical", "hold-critical", "sell-half-high", "unclear-high", "buy-high", "no-entry-high",
        "hold-info",
    ]


def test_sort_ties_primary_first_then_confidence(defaults: Defaults) -> None:
    alerts = [
        _alert("rumor", rumor=True, confidence=0.99),
        _alert("low", confidence=0.5),
        _alert("high", confidence=0.95),
        _alert("deterministic", confidence=None),
    ]
    assert [a.rule_id for a in sort_alerts(alerts, defaults.priority)] == ["deterministic", "high", "low", "rumor"]


def test_lead_alert(defaults: Defaults) -> None:
    assert lead_alert([], defaults.priority) is None
    lead = lead_alert([_alert("a", A.RECO_HOLD, S.HIGH), _alert("b", A.RECO_UNCLEAR, S.HIGH)], defaults.priority)
    assert lead.rule_id == "b"


# --------------------------------------------------------------------------- clés


def test_dedup_keys() -> None:
    entry = date(2026, 9, 25)
    assert dedup_key(_alert("U-B2"), entry) == "event:U-B2"
    assert dedup_key(_alert("U-B4", origin="price"), entry) == "price:U-B4:2026-09-25"
    assert dedup_key(_alert("U-B4", origin="price"), None) == "price:U-B4:-"
    assert dedup_key(_alert("U-B1-EXIT", origin="arm"), entry) == "arm:U-B1-EXIT"
    assert dedup_key(_alert("U-T2", origin="time"), entry) == "time:U-T2"
    assert dedup_key(_alert("P-ANOMALY", origin="anomaly"), entry) == "anomaly:2026-09-24"
    assert uses_window(_alert()) and not uses_window(_alert(origin="time"))


# --------------------------------------------------------------------------- décision


def test_first_occurrence_is_emitted(defaults: Defaults) -> None:
    assert should_emit(_alert(), [], defaults.priority) == (True, "nouvelle alerte")


@pytest.mark.parametrize("origin", ["price", "arm", "time", "anomaly"])
def test_deterministic_alerts_emitted_once(defaults: Defaults, origin: str) -> None:
    # Même une sévérité supérieure ne réémet pas une alerte déterministe.
    ok, _ = should_emit(_alert(origin=origin, severity=S.CRITICAL), [_alert(origin=origin)], defaults.priority)
    assert ok is False


def test_same_event_without_escalation_is_suppressed(defaults: Defaults) -> None:
    ok, why = should_emit(_alert(), [_alert()], defaults.priority)
    assert ok is False and "sans escalade" in why
    # Une rumeur après une confirmation primaire n'est pas une escalade.
    assert should_emit(_alert(rumor=True), [_alert()], defaults.priority)[0] is False


@pytest.mark.parametrize("new, old, reason", [
    (_alert(), _alert(rumor=True), "rumeur confirmée par une source primaire"),
    (_alert(severity=S.HIGH), _alert(severity=S.INFO), "sévérité INFO → HIGH"),
    (_alert(action=A.RECO_UNCLEAR), _alert(action=A.RECO_HOLD), "action RECO_HOLD → RECO_UNCLEAR"),
])
def test_escalations(defaults: Defaults, new: Alert, old: Alert, reason: str) -> None:
    assert escalation(new, old, defaults.priority) == reason
    assert should_emit(new, [old], defaults.priority) == (True, f"escalade : {reason}")


def test_escalation_must_beat_every_previous_alert(defaults: Defaults) -> None:
    previous = [_alert(rumor=True), _alert(severity=S.HIGH)]
    # Primaire INFO : escalade face à la rumeur, pas face à l'alerte HIGH déjà émise.
    assert should_emit(_alert(), previous, defaults.priority)[0] is False
    assert should_emit(_alert(severity=S.CRITICAL), previous, defaults.priority)[0] is True


def test_lower_severity_is_not_escalation(defaults: Defaults) -> None:
    assert escalation(_alert(severity=S.INFO), _alert(severity=S.HIGH), defaults.priority) is None
