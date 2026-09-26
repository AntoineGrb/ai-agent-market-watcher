from __future__ import annotations

from datetime import date

import pytest

from watcher.config import Action, Defaults, Severity
from watcher.engine.price_rules import (
    ARM_REF_FIGURE,
    evaluate_anomaly,
    evaluate_armed_watches,
    evaluate_price_rules,
)
from watcher.engine.time_rules import evaluate_time_rules
from watcher.store import ArmedWatch
from tests.conftest import LAST_CLOSE_DATE, repo_agent, snapshot

UBI = repo_agent("ubi")                  # entry_price 5,33 → doublement à 10,66
UBI_WATCH = repo_agent("ubi", status="WATCH")
NANO = repo_agent("nano")


def _watch(ref_value: float = 10.0, threshold: float = 0.98) -> ArmedWatch:
    return ArmedWatch(id=1, agent_id="UBI", arm_id="U-B1-EXIT", source_event_id=1, metric="price_vs_figure",
                      ref_value=ref_value, op=">=", threshold=threshold, action=Action.RECO_SELL_ALL,
                      severity=Severity.CRITICAL, created_at="2026-09-20T05:00:00+00:00")


# --------------------------------------------------------------------------- règles price


def test_price_rule_fires_at_double_entry() -> None:
    [alert] = evaluate_price_rules(UBI, UBI.rules, snapshot(10.66, 10.0), watch_active=False)
    assert (alert.rule_id, alert.source_rule_id, alert.origin) == ("U-B4", "U-B4", "price")
    assert (alert.action, alert.severity) == (Action.RECO_SELL_HALF, Severity.HIGH)
    assert alert.event_date == LAST_CLOSE_DATE
    assert alert.metrics == {"price_vs_entry": 2.0}
    assert alert.headline == "Cours ≥ 2 × prix d'entrée, sans offre en cours."
    assert "Clôture du 24/09/2026 : 10,66 EUR" in alert.rationale
    assert alert.confidence is None and alert.evidence == []


@pytest.mark.parametrize("cfg, price, watch_active", [
    (UBI, snapshot(10.6, 10.0), False),              # sous le seuil
    (UBI, snapshot(11.0, 10.0), True),               # unless_armed : une offre est en cours
    (UBI, snapshot(11.0, 10.0, new=False), False),   # pas de nouvelle clôture
    (UBI, None, False),                              # cours indisponible
    (UBI_WATCH, snapshot(11.0, 10.0), False),        # position non détenue
])
def test_price_rule_not_fired(cfg, price, watch_active: bool) -> None:
    assert evaluate_price_rules(cfg, cfg.rules, price, watch_active=watch_active) == []


def test_price_rule_unavailable_metric_is_skipped(caplog) -> None:
    broken = repo_agent("ubi", entry_price=None)   # contourne la validation : cas défensif
    assert evaluate_price_rules(broken, broken.rules, snapshot(11.0, 10.0), watch_active=False) == []
    assert "U-B4 non évaluable" in caplog.text


# --------------------------------------------------------------------------- surveillances armées


def test_armed_watch_fires() -> None:
    watch = _watch(ref_value=10.0)
    [(fired, alert)] = evaluate_armed_watches(UBI, [watch], snapshot(9.8, 9.5))
    assert fired is watch
    assert (alert.rule_id, alert.source_rule_id, alert.origin) == ("U-B1-EXIT", "U-B1-EXIT", "arm")
    assert (alert.action, alert.severity) == (Action.RECO_SELL_ALL, Severity.CRITICAL)
    assert alert.figures == {ARM_REF_FIGURE: 10.0}
    assert alert.metrics == {"price_vs_figure(ref_value)": pytest.approx(0.98)}
    assert alert.headline == "Cours à 98,0 % du prix de référence (10,00 EUR)"


@pytest.mark.parametrize("cfg, price, watch", [
    (UBI, snapshot(9.7, 9.5), _watch()),
    (UBI, snapshot(9.9, 9.5, new=False), _watch()),
    (UBI_WATCH, snapshot(9.9, 9.5), _watch()),
    (UBI, None, _watch()),
    (UBI, snapshot(9.9, 9.5), _watch(ref_value=0.0)),   # référence invalide : non évaluable
])
def test_armed_watch_not_fired(cfg, price, watch: ArmedWatch) -> None:
    assert evaluate_armed_watches(cfg, [watch], price) == []


# --------------------------------------------------------------------------- anomalie


@pytest.mark.parametrize("cfg", [UBI, UBI_WATCH], ids=["OWNED", "WATCH"])
def test_anomaly_fires_on_unexplained_move(defaults: Defaults, cfg) -> None:
    alert = evaluate_anomaly(cfg, defaults, snapshot(12.5, 10.0), explained=False)
    assert (alert.rule_id, alert.origin) == ("P-ANOMALY", "anomaly")
    assert (alert.action, alert.severity) == (Action.RECO_UNCLEAR, Severity.HIGH)
    assert alert.headline == "Mouvement inexpliqué de +25,0 %, cherche la source"
    assert alert.metrics == {"daily_move_pct": pytest.approx(25.0)}
    assert alert.event_date == LAST_CLOSE_DATE


def test_anomaly_threshold_is_inclusive(defaults: Defaults) -> None:
    # 8 / 10 − 1 = −19,999999999999996 % en flottant.
    alert = evaluate_anomaly(UBI, defaults, snapshot(8.0, 10.0), explained=False)
    assert alert is not None and alert.headline.startswith("Mouvement inexpliqué de -20,0 %")


@pytest.mark.parametrize("price, explained", [
    (snapshot(11.9, 10.0), False),             # +19 %
    (snapshot(12.5, 10.0), True),              # expliqué par une actualité
    (snapshot(12.5, 10.0, new=False), False),  # pas de nouvelle clôture
    (snapshot(12.5, None), False),             # variation inconnue
    (None, False),
])
def test_anomaly_not_fired(defaults: Defaults, price, explained: bool) -> None:
    assert evaluate_anomaly(UBI, defaults, price, explained=explained) is None


# --------------------------------------------------------------------------- règles time


def test_max_holding_days() -> None:
    # U-T2 : 365 jours après l'entrée du 25/09/2026.
    active = UBI.active_rules(set())
    assert evaluate_time_rules(UBI, active, date(2027, 9, 24)) == []
    [alert] = evaluate_time_rules(UBI, active, date(2027, 9, 25))
    assert (alert.rule_id, alert.origin, alert.action, alert.severity) == (
        "U-T2", "time", Action.RECO_SELL_ALL, Severity.HIGH)
    assert alert.event_date == date(2027, 9, 25)
    assert "durée de détention maximale de 365 jours atteinte le 25/09/2027" in alert.rationale


def test_deadline_with_unless_fired() -> None:
    today = date(2027, 6, 30)
    assert evaluate_time_rules(UBI_WATCH, UBI_WATCH.active_rules(set()), date(2027, 6, 29)) == []
    [alert] = evaluate_time_rules(UBI_WATCH, UBI_WATCH.active_rules(set()), today)
    assert (alert.rule_id, alert.action, alert.severity) == ("U-T1", Action.RECO_NO_ENTRY, Severity.HIGH)
    assert alert.rationale == "Date butoir du 30/06/2027 atteinte."
    # Un refinancement déjà détecté (U-E1) neutralise le time stop.
    assert evaluate_time_rules(UBI_WATCH, UBI_WATCH.active_rules({"U-E1"}), today) == []


def test_nano_time_stop_neutralized_by_result() -> None:
    today = date(2029, 1, 1)
    assert [a.rule_id for a in evaluate_time_rules(NANO, NANO.active_rules(set()), today)] == ["N-T1"]
    assert evaluate_time_rules(NANO, NANO.active_rules({"N-B1"}), today) == []


def test_max_holding_without_entry_date_is_skipped(caplog) -> None:
    broken = repo_agent("ubi", entry_date=None)
    assert evaluate_time_rules(broken, broken.active_rules(set()), date(2026, 10, 1)) == []
    assert "U-T2 non évaluable" in caplog.text
