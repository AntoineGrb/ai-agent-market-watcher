from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

import pytest

from watcher.config import Action, AgentConfig, Defaults, Severity
from watcher.engine.pipeline import AgentEvaluation, evaluate_agent
from watcher.models import PriceSnapshot, RuleMatch
from watcher.store import Store
from tests.conftest import NOW, items_by_id, news, repo_agent, rule_match, snapshot

UBI = repo_agent("ubi")
NANO = repo_agent("nano")
ITEMS = items_by_id([news("a"), news("b"), news("p", primary=False), news("n", agent_id="NANO")])
QUIET = snapshot(10.0, 9.8)   # +2 % : ni anomalie, ni promotion de rumeur


def evaluate(store: Store, defaults: Defaults, cfg: AgentConfig = UBI, matches: Sequence[RuleMatch] = (), *,
             price: PriceSnapshot | None = QUIET, now: datetime = NOW) -> AgentEvaluation:
    return evaluate_agent(store, cfg, defaults, matches=list(matches), items=ITEMS, price=price, now=now,
                          fired=store.fired_rule_ids(cfg.agent_id))


def _ids(alerts) -> list[str]:
    return [a.rule_id for a in alerts]


def _watches(store: Store) -> list[tuple[str, str]]:
    rows = store._conn.execute("SELECT arm_id, status FROM armed_watches ORDER BY id").fetchall()
    return [(r["arm_id"], r["status"]) for r in rows]


# --------------------------------------------------------------------------- événements


def test_event_written_to_outbox(store: Store, defaults: Defaults) -> None:
    result = evaluate(store, defaults, matches=[rule_match("U-S2")])
    assert _ids(result.emitted) == ["U-S2"]
    [row] = store.pending_events()
    assert (row.dedup_key, row.sent_at, row.alert.action) == ("event:U-S2", None, Action.RECO_SELL_ALL)
    assert store.fired_rule_ids("UBI") == {"U-S2"}


def test_event_dedup_window(store: Store, defaults: Defaults) -> None:
    window = defaults.dedup.event_window_days
    evaluate(store, defaults, matches=[rule_match("U-S5")])
    later = NOW + timedelta(days=window - 1)
    result = evaluate(store, defaults, matches=[rule_match("U-S5", event_date=later.date())], now=later)
    assert result.emitted == [] and _ids(result.suppressed) == ["U-S5"]
    later = NOW + timedelta(days=window + 1)
    result = evaluate(store, defaults, matches=[rule_match("U-S5", event_date=later.date())], now=later)
    assert _ids(result.emitted) == ["U-S5"]


def test_rumor_then_primary_is_escalated(store: Store, defaults: Defaults) -> None:
    first = evaluate(store, defaults, matches=[rule_match("U-S2", ["p"])])
    assert first.emitted[0].is_rumor and first.emitted[0].action is Action.RECO_UNCLEAR
    tomorrow = NOW + timedelta(days=1)
    second = evaluate(store, defaults, matches=[rule_match("U-S2", ["a"])], now=tomorrow)
    assert [(a.is_rumor, a.action) for a in second.emitted] == [(False, Action.RECO_SELL_ALL)]
    # La rumeur qui continue de circuler ensuite n'est plus remontée.
    third = evaluate(store, defaults, matches=[rule_match("U-S2", ["p"])], now=tomorrow + timedelta(days=1))
    assert third.emitted == []


def test_best_match_of_a_run_wins_dedup(store: Store, defaults: Defaults) -> None:
    result = evaluate(store, defaults, matches=[rule_match("U-S2", ["p"]), rule_match("U-S2", ["a"])])
    assert [(a.rule_id, a.is_rumor) for a in result.emitted] == [("U-S2", False)]
    assert [(a.rule_id, a.is_rumor) for a in result.suppressed] == [("U-S2", True)]


def test_ignore_match_is_not_sent_and_explains_move(store: Store, defaults: Defaults) -> None:
    result = evaluate(store, defaults, NANO, [rule_match("N-N3", ["n"])], price=snapshot(30.0, 24.0))
    assert _ids(result.ignored) == ["N-N3"]
    assert result.emitted == [] and store.pending_events() == []


# --------------------------------------------------------------------------- surveillances armées


def test_offer_arms_watch_and_fires_immediately(store: Store, defaults: Defaults) -> None:
    # Offre à 11 € annoncée le 24 (veille : 9,5) ; le cours a déjà clôturé à 10,8 ≥ 0,98 × 11.
    result = evaluate(store, defaults, matches=[rule_match("U-B1", offer_price=11.0)], price=snapshot(10.8, 9.5))
    assert result.armed == ["U-B1-EXIT"]
    assert _ids(result.emitted) == ["U-B1", "U-B1-EXIT"]
    assert result.emitted[1].action is Action.RECO_SELL_ALL
    assert _watches(store) == [("U-B1-EXIT", "fired")]


def test_armed_watch_fires_on_later_run_once(store: Store, defaults: Defaults) -> None:
    evaluate(store, defaults, matches=[rule_match("U-B1", offer_price=11.0)], price=snapshot(10.0, 9.5))
    assert _watches(store) == [("U-B1-EXIT", "active")]
    [row] = store.active_armed_watches("UBI")
    assert (row.ref_value, row.op, row.threshold, row.action) == (11.0, ">=", 0.98, Action.RECO_SELL_ALL)

    day2 = NOW + timedelta(days=1)
    result = evaluate(store, defaults, price=snapshot(10.6, 10.0, new=False), now=day2)   # pas de séance
    assert result.emitted == []
    day3 = NOW + timedelta(days=4)
    result = evaluate(store, defaults, price=snapshot(10.85, 10.6, last_date=day3.date()), now=day3)
    assert _ids(result.emitted) == ["U-B1-EXIT"]
    assert _watches(store) == [("U-B1-EXIT", "fired")]
    # Sous le double du prix d'entrée (10,66) : seule la surveillance, déjà déclenchée, pourrait alerter.
    result = evaluate(store, defaults, price=snapshot(10.6, 10.85, last_date=day3.date() + timedelta(days=1)),
                      now=day3 + timedelta(days=1))
    assert result.emitted == []


def test_duplicate_offer_does_not_rearm(store: Store, defaults: Defaults) -> None:
    evaluate(store, defaults, matches=[rule_match("U-B1", offer_price=11.0)], price=snapshot(10.0, 9.5))
    later = NOW + timedelta(days=2)
    result = evaluate(store, defaults, matches=[rule_match("U-B1", ["b"], offer_price=11.0,
                                                           event_date=later.date())],
                      price=snapshot(10.0, 10.0, last_date=later.date()), now=later)
    assert _ids(result.suppressed) == ["U-B1"] and result.armed == []
    assert _watches(store) == [("U-B1-EXIT", "active")]


def test_active_watch_neutralizes_unless_armed_price_rule(store: Store, defaults: Defaults) -> None:
    # Offre à 11 € et cours au double du prix d'entrée (10,66) mais sous 0,98 × 11 = 10,78.
    result = evaluate(store, defaults, matches=[rule_match("U-B1", offer_price=11.0)], price=snapshot(10.7, 9.5))
    assert _ids(result.emitted) == ["U-B1"]


def test_rumor_offer_does_not_arm(store: Store, defaults: Defaults) -> None:
    result = evaluate(store, defaults, matches=[rule_match("U-B1", ["p"], offer_price=11.0)],
                      price=snapshot(10.0, 9.5))
    assert result.armed == [] and _watches(store) == []
    assert _ids(result.emitted) == ["U-B1"]


# --------------------------------------------------------------------------- price / time


def test_price_rule_once_per_entry_date(store: Store, defaults: Defaults) -> None:
    assert _ids(evaluate(store, defaults, price=snapshot(10.7, 10.5)).emitted) == ["U-B4"]
    later = NOW + timedelta(days=3)
    assert evaluate(store, defaults, price=snapshot(11.0, 10.7, last_date=later.date()), now=later).emitted == []
    # Nouvelle position (nouveau prix et nouvelle date d'entrée) : la règle est réarmée.
    reopened = repo_agent("ubi", entry_price=5.0, entry_date=later.date())
    result = evaluate(store, defaults, reopened, price=snapshot(11.0, 10.7, last_date=later.date()), now=later)
    assert _ids(result.emitted) == ["U-B4"]


def test_time_rule_never_repeated(store: Store, defaults: Defaults) -> None:
    due = NOW + timedelta(days=365)
    assert _ids(evaluate(store, defaults, price=None, now=due).emitted) == ["U-T2"]
    assert evaluate(store, defaults, price=None, now=due + timedelta(days=40)).emitted == []


# --------------------------------------------------------------------------- anomalie


def test_anomaly_once_per_close(store: Store, defaults: Defaults) -> None:
    jump = snapshot(30.0, 24.0, symbol="NANO.PA")   # +25 %, loin du double du prix d'entrée
    assert _ids(evaluate(store, defaults, NANO, price=jump).emitted) == ["P-ANOMALY"]
    # Run relancé sur la même clôture (ex. --agent à la main) : pas de seconde alerte.
    assert evaluate(store, defaults, NANO, price=jump, now=NOW + timedelta(hours=1)).emitted == []


def test_match_in_run_explains_anomaly(store: Store, defaults: Defaults) -> None:
    result = evaluate(store, defaults, matches=[rule_match("U-S5")], price=snapshot(7.5, 10.0))
    assert _ids(result.emitted) == ["U-S5"]


@pytest.mark.parametrize("days_ago, anomaly", [(2, False), (4, True)])
def test_quiet_days(store: Store, defaults: Defaults, days_ago: int, anomaly: bool) -> None:
    assert defaults.price_anomaly.quiet_days == 3
    past = NOW - timedelta(days=days_ago)
    evaluate(store, defaults, matches=[rule_match("U-N1", event_date=past.date())], price=None, now=past)
    result = evaluate(store, defaults, price=snapshot(7.5, 10.0))
    assert ("P-ANOMALY" in _ids(result.emitted)) is anomaly


def test_deterministic_alerts_do_not_explain_anomaly(store: Store, defaults: Defaults) -> None:
    yesterday = NOW - timedelta(days=1)
    first = evaluate(store, defaults, price=snapshot(12.5, 10.0, last_date=date(2026, 9, 23)), now=yesterday)
    assert _ids(first.emitted) == ["U-B4", "P-ANOMALY"]
    result = evaluate(store, defaults, price=snapshot(9.5, 12.5))
    assert _ids(result.emitted) == ["P-ANOMALY"]
    assert result.emitted[0].severity is Severity.HIGH


# --------------------------------------------------------------------------- transaction


def test_evaluation_is_rolled_back_with_agent_transaction(store: Store, defaults: Defaults) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            evaluate(store, defaults, matches=[rule_match("U-B1", offer_price=11.0)], price=snapshot(10.0, 9.5))
            raise RuntimeError("échec plus loin dans le run de l'agent")
    assert store.pending_events() == [] and _watches(store) == []


def test_offer_reported_after_window_keeps_single_watch(store: Store, defaults: Defaults) -> None:
    evaluate(store, defaults, matches=[rule_match("U-B1", offer_price=11.0)], price=snapshot(10.0, 9.5))
    later = NOW + timedelta(days=defaults.dedup.event_window_days + 1)
    result = evaluate(store, defaults, matches=[rule_match("U-B1", ["b"], offer_price=11.0,
                                                           event_date=later.date())],
                      price=snapshot(10.0, 10.0, last_date=later.date()), now=later)
    assert _ids(result.emitted) == ["U-B1"]          # hors fenêtre : l'événement est réémis...
    assert result.armed == []                        # ...mais la surveillance déjà active n'est pas doublée
    assert _watches(store) == [("U-B1-EXIT", "active")]
