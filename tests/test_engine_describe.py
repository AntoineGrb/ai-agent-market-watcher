from __future__ import annotations

from datetime import date

import pytest

from watcher.config import Action, Defaults, Severity
from watcher.engine.describe import metric_text, num, rule_text
from watcher.engine.pipeline import evaluate_agent
from watcher.models import Alert
from watcher.store import Store
from tests.conftest import LAST_CLOSE_DATE, NOW, news, repo_agent, rule_match, snapshot


def _alert(rule_id: str, origin: str, source_rule_id: str | None = None) -> Alert:
    return Alert(agent_id="UBI", rule_id=rule_id, source_rule_id=source_rule_id or rule_id, origin=origin,
                 action=Action.RECO_HOLD, severity=Severity.INFO, headline="h", rationale="r",
                 event_date=LAST_CLOSE_DATE)


@pytest.mark.parametrize(("label", "text"), [
    ("price_vs_entry", "cours / prix d'entrée"),
    ("dilution_pct", "dilution (%)"),
    ("figure_vs_prev_close(offer_price)", "offer_price / clôture précédant l'événement"),
    ("price_vs_figure(offer_price)", "cours / offer_price"),
    ("price_vs_figure(ref_value)", "cours / prix de référence"),
    ("figure(fcf_guidance_low_meur)", "fcf_guidance_low_meur"),
    ("daily_move_pct", "variation J-1 (%)"),
    ("inconnue(x)", "inconnue(x)"),
    ("pas un libellé !", "pas un libellé !"),
])
def test_metric_text(label: str, text: str) -> None:
    assert metric_text(label) == text


def test_num() -> None:
    assert (num(0.98), num(20.0), num(-500.0), num(1.5, 2)) == ("0,98", "20", "-500", "1,50")


def test_rule_text_per_origin(defaults: Defaults) -> None:
    ubi = repo_agent("ubi")
    assert rule_text(_alert("U-S2", "event"), ubi, defaults).startswith("Bris de covenant sans waiver obtenu")
    assert rule_text(_alert("U-B2", "event", "U-B1"), ubi, defaults) == (
        "Offre publique (OPA, OPR, OPAS) déposée ou annoncée sur les titres Ubisoft.")
    assert rule_text(_alert("U-B4", "price"), ubi, defaults) == (
        "cours / prix d'entrée ≥ 2, sans surveillance armée active")
    assert rule_text(_alert("U-B1-EXIT", "arm"), ubi, defaults) == (
        "surveillance armée par U-B1 : cours / offer_price ≥ 0,98")
    assert rule_text(_alert("U-T1", "time"), ubi, defaults) == "date butoir : 30/06/2027"
    assert rule_text(_alert("U-T2", "time"), ubi, defaults) == "durée de détention maximale : 365 jours"
    assert rule_text(_alert("P-ANOMALY", "anomaly"), ubi, defaults) == (
        "|variation J-1| ≥ 20 % sans actualité associée (ni match ce run, ni événement depuis 3 jours)")
    # Règle disparue de la config : pas de texte plutôt qu'un texte inventé.
    assert rule_text(_alert("U-X9", "event"), ubi, defaults) is None
    assert rule_text(_alert("U-X9-EXIT", "arm"), ubi, defaults) is None


def test_price_rule_without_unless_armed(defaults: Defaults) -> None:
    ubi = repo_agent("ubi")
    rules = [r.model_copy(update={"unless_armed": False}) if r.id == "U-B4" else r for r in ubi.rules]
    ubi = ubi.model_copy(update={"rules": rules})
    assert rule_text(_alert("U-B4", "price"), ubi, defaults) == "cours / prix d'entrée ≥ 2"


def test_emitted_alerts_carry_mail_context(store: Store, defaults: Defaults) -> None:
    """Texte de la règle, seuils évalués, devise, prix d'entrée et cours sont figés dans l'alerte."""
    ubi = repo_agent("ubi", entry_date=date(2025, 9, 1))   # time stop U-T2 échu
    evaluation = evaluate_agent(store, ubi, defaults, matches=[rule_match("U-B1", offer_price=11.0)],
                                items={"a": news("a")}, price=snapshot(10.8, 9.5), now=NOW, fired=set())
    by_id = {a.rule_id: a for a in evaluation.emitted}
    assert set(by_id) == {"U-B1", "U-B1-EXIT", "U-T2"}

    event = by_id["U-B1"]
    assert event.rule_text.startswith("Offre publique")
    assert (event.currency, event.entry_price) == ("EUR", 5.33)
    [check] = event.checks
    assert (check.metric, check.op, check.threshold, check.met) == ("figure_vs_prev_close(offer_price)", ">=", 1.0,
                                                                     True)
    assert check.value == pytest.approx(11.0 / 9.5, abs=1e-6)

    arm = by_id["U-B1-EXIT"]
    assert arm.rule_text == "surveillance armée par U-B1 : cours / offer_price ≥ 0,98"
    assert [(c.metric, c.met) for c in arm.checks] == [("price_vs_figure(ref_value)", True)]

    time_stop = by_id["U-T2"]
    assert time_stop.rule_text == "durée de détention maximale : 365 jours"
    assert time_stop.price is not None and time_stop.price.last_close == 10.8   # contexte de cours ajouté

    # Le contexte est bien persisté dans l'outbox.
    stored = {r.alert.rule_id: r.alert for r in store.pending_events()}
    assert stored["U-B1"].checks == event.checks and stored["U-T2"].rule_text == time_stop.rule_text


def test_watch_position_has_no_entry_price(store: Store, defaults: Defaults) -> None:
    ubi = repo_agent("ubi", status="WATCH")
    evaluation = evaluate_agent(store, ubi, defaults, matches=[rule_match("U-E3")], items={"a": news("a")},
                                price=snapshot(10.0, 9.5), now=NOW, fired=set())
    [alert] = evaluation.emitted
    assert alert.entry_price is None and alert.currency == "EUR"
