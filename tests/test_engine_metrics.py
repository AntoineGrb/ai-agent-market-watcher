from __future__ import annotations

from datetime import date

import pytest

from watcher.config import Condition
from watcher.engine.metrics import (
    MetricContext,
    MetricUnavailable,
    compare,
    daily_move_pct,
    dilution_pct,
    metric_label,
)
from tests.conftest import LAST_CLOSE_DATE, repo_agent, snapshot

UBI = repo_agent("ubi")                         # OWNED, entry_price 5.33, 136 237 168 actions
NO_SHARES = repo_agent("ubi", shares_outstanding=None)


def _cond(metric: str, op: str = ">=", value: float = 1.0, figure: str | None = None) -> Condition:
    return Condition(metric=metric, op=op, value=value, figure=figure)


# --------------------------------------------------------------------------- dilution_pct


def test_dilution_reference_example() -> None:
    # Exemple de référence du cadrage : 40 M d'actions nouvelles pour 142 M existantes → 21,98 %.
    assert dilution_pct(40_000_000, 142_000_000) == pytest.approx(21.978, abs=1e-3)
    ctx = MetricContext(UBI.position, figures={"new_shares": 40_000_000, "existing_shares": 142_000_000})
    assert ctx.check(_cond("dilution_pct", ">", 20)) is True
    assert ctx.computed["dilution_pct"] == pytest.approx(21.978022)


def test_dilution_falls_back_on_position_shares() -> None:
    ctx = MetricContext(UBI.position, figures={"new_shares": 13_623_717})
    assert ctx.value(_cond("dilution_pct")) == pytest.approx(100 * 13_623_717 / (136_237_168 + 13_623_717))


def test_dilution_zero_new_shares_needs_no_existing() -> None:
    ctx = MetricContext(NO_SHARES.position, figures={"new_shares": 0})
    assert ctx.value(_cond("dilution_pct")) == 0.0


@pytest.mark.parametrize("figures, message", [
    ({}, "new_shares non extrait"),
    ({"new_shares": 1_000_000}, "actions existantes inconnu"),
    ({"new_shares": -5}, "négatif"),
    ({"new_shares": 1_000_000, "existing_shares": 0}, "actions existantes inconnu"),
])
def test_dilution_unavailable(figures: dict[str, float], message: str) -> None:
    ctx = MetricContext(NO_SHARES.position, figures=figures)
    with pytest.raises(MetricUnavailable, match=message):
        ctx.check(_cond("dilution_pct", ">", 20))
    assert ctx.computed == {}


# --------------------------------------------------------------------------- price_vs_entry


def test_price_vs_entry_exact_double_is_reached() -> None:
    # 10,66 / 5,33 vaut 1,9999999999999998 en flottant : l'arrondi avant comparaison évite le faux négatif.
    ctx = MetricContext(UBI.position, snapshot(last=10.66))
    assert ctx.check(_cond("price_vs_entry", ">=", 2.0)) is True
    assert ctx.computed["price_vs_entry"] == 2.0


def test_price_vs_entry_unavailable() -> None:
    with pytest.raises(MetricUnavailable, match="cours indisponible"):
        MetricContext(UBI.position).value(_cond("price_vs_entry"))
    watch = repo_agent("ubi", status="WATCH")
    with pytest.raises(MetricUnavailable, match="aucune position détenue"):
        MetricContext(watch.position, snapshot()).value(_cond("price_vs_entry"))


# --------------------------------------------------------------------------- figure_vs_prev_close


def test_figure_vs_prev_close_uses_session_strictly_before_event() -> None:
    price = snapshot(last=10.8, prev=10.0)   # 23/09 : 10,0 ; 24/09 : 10,8
    cond = _cond("figure_vs_prev_close", figure="offer_price")
    # Offre annoncée le 24 : référence = clôture du 23, pas celle du jour de l'annonce.
    ctx = MetricContext(UBI.position, price, {"offer_price": 11.0}, LAST_CLOSE_DATE)
    assert ctx.value(cond) == pytest.approx(1.1)
    # Annonce un samedi : référence = dernière séance (jeudi 24).
    ctx = MetricContext(UBI.position, price, {"offer_price": 11.0}, date(2026, 9, 26))
    assert ctx.value(cond) == pytest.approx(11.0 / 10.8)


@pytest.mark.parametrize("kwargs, message", [
    ({"figures": {}}, "offer_price non extrait"),
    ({"event_date": None}, "date de l'événement inconnue"),
    ({"price": None}, "cours indisponible"),
    ({"event_date": date(2026, 9, 22)}, "aucune clôture antérieure"),
    ({"price": snapshot(history=[(date(2026, 9, 23), 0.0), (LAST_CLOSE_DATE, 10.0)])}, "référence invalide"),
])
def test_figure_vs_prev_close_unavailable(kwargs: dict, message: str) -> None:
    base = {"price": snapshot(), "figures": {"offer_price": 11.0}, "event_date": LAST_CLOSE_DATE}
    ctx = MetricContext(UBI.position, **{**base, **kwargs})
    with pytest.raises(MetricUnavailable, match=message):
        ctx.value(_cond("figure_vs_prev_close", figure="offer_price"))


# --------------------------------------------------------------------------- price_vs_figure, figure


def test_price_vs_figure() -> None:
    cond = _cond("price_vs_figure", ">=", 0.98, figure="offer_price")
    assert MetricContext(UBI.position, snapshot(last=9.8), {"offer_price": 10.0}).check(cond) is True
    assert MetricContext(UBI.position, snapshot(last=9.7), {"offer_price": 10.0}).check(cond) is False
    with pytest.raises(MetricUnavailable, match="référence invalide"):
        MetricContext(UBI.position, snapshot(), {"offer_price": 0}).value(cond)
    with pytest.raises(MetricUnavailable, match="cours indisponible"):
        MetricContext(UBI.position, None, {"offer_price": 10.0}).value(cond)


def test_raw_figure() -> None:
    cond = _cond("figure", "<", -500, figure="fcf_guidance_low_meur")
    ctx = MetricContext(UBI.position, figures={"fcf_guidance_low_meur": -600})
    assert ctx.check(cond) is True
    assert ctx.computed == {"figure(fcf_guidance_low_meur)": -600}
    with pytest.raises(MetricUnavailable, match="non extrait"):
        MetricContext(UBI.position).value(cond)
    with pytest.raises(MetricUnavailable, match="invalide"):
        MetricContext(UBI.position, figures={"fcf_guidance_low_meur": float("nan")}).value(cond)


# --------------------------------------------------------------------------- utilitaires


def test_daily_move_pct() -> None:
    assert daily_move_pct(10.0, 12.5) == pytest.approx(25.0)
    assert daily_move_pct(10.0, 8.0) == pytest.approx(-20.0)
    assert daily_move_pct(None, 8.0) is None
    assert daily_move_pct(0.0, 8.0) is None


@pytest.mark.parametrize("value, op, threshold, expected", [
    (2.0, ">=", 2.0, True), (2.0, ">", 2.0, False), (2.0, "<=", 2.0, True), (2.0, "<", 2.0, False),
    (1.9999999999999998, ">=", 2.0, True), (20.0000001, ">", 20, True),
])
def test_compare(value: float, op: str, threshold: float, expected: bool) -> None:
    assert compare(value, op, threshold) is expected


def test_metric_label() -> None:
    assert metric_label(_cond("dilution_pct")) == "dilution_pct"
    assert metric_label(_cond("figure", figure="offer_price")) == "figure(offer_price)"
