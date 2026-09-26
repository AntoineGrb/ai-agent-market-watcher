from __future__ import annotations

from datetime import timedelta

import pytest

from watcher.config import Action, AgentConfig, Defaults, EventRule, Severity
from watcher.engine.resolve import ResolvedMatch, resolve_matches
from watcher.models import PriceSnapshot, RuleMatch
from tests.conftest import LAST_CLOSE_DATE, TODAY, items_by_id, news, repo_agent, rule_match, snapshot

A, S, _ = Action, Severity, None
UBI = repo_agent("ubi")
UBI_WATCH = repo_agent("ubi", status="WATCH")
NANO = repo_agent("nano")                      # entry_price 22,55 ; 50 941 528 actions
ITEMS = items_by_id([news("a"), news("b"), news("p", primary=False), news("q", primary=False)])
DEFAULT_PRICE = snapshot(last=10.0, prev=9.5)   # clôture du 23/09 (veille de l'événement) : 9,5
NO_PRICE: PriceSnapshot | None = None


def resolve(cfg: AgentConfig, defaults: Defaults, *matches: RuleMatch, price: PriceSnapshot | None = DEFAULT_PRICE,
            fired: set[str] | None = None) -> list[ResolvedMatch]:
    return resolve_matches(list(matches), cfg=cfg, defaults=defaults, items=ITEMS, price=price, today=TODAY,
                           fired=fired or set())


def one(cfg: AgentConfig, defaults: Defaults, match: RuleMatch, **kwargs) -> ResolvedMatch:
    resolved = resolve(cfg, defaults, match, **kwargs)
    assert len(resolved) == 1
    return resolved[0]


# --------------------------------------------------------------------------- chemins d'override des configs


OVERRIDE_PATHS = [
    # agent, règle, chiffres extraits, cours, → ID affiché, action, sévérité, surveillance armée
    ("U-E1 sans dilution", UBI_WATCH, "U-E1", {"new_shares": 0}, DEFAULT_PRICE, "U-E1", A.RECO_BUY, S.HIGH, _),
    ("U-E1 dilution faible", UBI_WATCH, "U-E1", {"new_shares": 5e6}, DEFAULT_PRICE, "U-E1", A.RECO_BUY, S.HIGH, _),
    ("U-E2 dilution > 20 %", UBI_WATCH, "U-E1", {"new_shares": 40e6, "existing_shares": 142e6}, DEFAULT_PRICE,
     "U-E2", A.RECO_NO_ENTRY, S.INFO, _),
    ("U-E1 chiffre absent", UBI_WATCH, "U-E1", {}, DEFAULT_PRICE, "U-E1", A.RECO_UNCLEAR, S.HIGH, _),
    ("U-E3", UBI_WATCH, "U-E3", {}, DEFAULT_PRICE, "U-E3", A.RECO_NO_ENTRY, S.INFO, _),
    ("U-B1 offre avec prime", UBI, "U-B1", {"offer_price": 11.0}, DEFAULT_PRICE,
     "U-B1", A.RECO_HOLD, S.CRITICAL, "U-B1-EXIT"),
    ("U-B1 offre au cours", UBI, "U-B1", {"offer_price": 9.5}, DEFAULT_PRICE,
     "U-B1", A.RECO_HOLD, S.CRITICAL, "U-B1-EXIT"),
    ("U-B2 offre à décote", UBI, "U-B1", {"offer_price": 9.0}, DEFAULT_PRICE, "U-B2", A.RECO_UNCLEAR, S.CRITICAL, _),
    ("U-B1 prix absent", UBI, "U-B1", {}, DEFAULT_PRICE, "U-B1", A.RECO_UNCLEAR, S.HIGH, _),
    ("U-B1 cours absent", UBI, "U-B1", {"offer_price": 11.0}, NO_PRICE, "U-B1", A.RECO_UNCLEAR, S.HIGH, _),
    ("U-B3", UBI, "U-B3", {}, DEFAULT_PRICE, "U-B3", A.RECO_HOLD, S.HIGH, _),
    ("U-S1 dilution > 20 %", UBI, "U-S1", {"new_shares": 40e6}, DEFAULT_PRICE,
     "U-S1", A.RECO_SELL_ALL, S.CRITICAL, _),
    ("U-S1 dilution faible", UBI, "U-S1", {"new_shares": 10e6}, DEFAULT_PRICE, "U-S1", A.RECO_HOLD, S.INFO, _),
    ("U-S1 chiffre absent", UBI, "U-S1", {}, DEFAULT_PRICE, "U-S1", A.RECO_UNCLEAR, S.HIGH, _),
    ("U-S2", UBI, "U-S2", {}, DEFAULT_PRICE, "U-S2", A.RECO_SELL_ALL, S.CRITICAL, _),
    ("U-S3 FCF < -500", UBI, "U-S3", {"fcf_guidance_low_meur": -600}, DEFAULT_PRICE,
     "U-S3", A.RECO_SELL_ALL, S.CRITICAL, _),
    ("U-S3 retour FCF après 2028", UBI, "U-S3", {"fcf_guidance_low_meur": -300, "fcf_positive_fy_end_year": 2029},
     DEFAULT_PRICE, "U-S3", A.RECO_SELL_ALL, S.CRITICAL, _),
    ("U-S3 horizon absent (skip)", UBI, "U-S3", {"fcf_guidance_low_meur": -300}, DEFAULT_PRICE,
     "U-S3", A.RECO_HOLD, S.INFO, _),
    ("U-S3 FCF absent, horizon > 2028", UBI, "U-S3", {"fcf_positive_fy_end_year": 2029}, DEFAULT_PRICE,
     "U-S3", A.RECO_SELL_ALL, S.CRITICAL, _),
    ("U-S3 FCF absent, horizon 2028", UBI, "U-S3", {"fcf_positive_fy_end_year": 2028}, DEFAULT_PRICE,
     "U-S3", A.RECO_UNCLEAR, S.HIGH, _),
    ("U-S3 aucun chiffre", UBI, "U-S3", {}, DEFAULT_PRICE, "U-S3", A.RECO_UNCLEAR, S.HIGH, _),
    ("U-S5", UBI, "U-S5", {}, DEFAULT_PRICE, "U-S5", A.RECO_UNCLEAR, S.HIGH, _),
    ("U-N1", UBI, "U-N1", {}, DEFAULT_PRICE, "U-N1", A.RECO_HOLD, S.INFO, _),
    ("N-B1 cours ≥ 2 × entrée", NANO, "N-B1", {}, snapshot(45.2, 40.0), "N-B1", A.RECO_SELL_HALF, S.CRITICAL, _),
    ("N-B1 cours < 2 × entrée", NANO, "N-B1", {}, snapshot(30.0, 25.0), "N-B1", A.RECO_UNCLEAR, S.CRITICAL, _),
    ("N-B1 cours absent", NANO, "N-B1", {}, NO_PRICE, "N-B1", A.RECO_UNCLEAR, S.HIGH, _),
    ("N-B3 offre avec prime", NANO, "N-B3", {"offer_price": 12.0}, DEFAULT_PRICE,
     "N-B3", A.RECO_HOLD, S.CRITICAL, "N-B3-EXIT"),
    ("N-B3 offre à décote", NANO, "N-B3", {"offer_price": 9.0}, DEFAULT_PRICE,
     "N-B3-DECOTE", A.RECO_UNCLEAR, S.CRITICAL, _),
    ("N-B3 prix absent", NANO, "N-B3", {}, DEFAULT_PRICE, "N-B3", A.RECO_UNCLEAR, S.HIGH, _),
    ("N-S1", NANO, "N-S1", {}, DEFAULT_PRICE, "N-S1", A.RECO_SELL_ALL, S.CRITICAL, _),
    ("N-S4 dilution > 20 %", NANO, "N-S4", {"new_shares": 15e6}, DEFAULT_PRICE, "N-S4", A.RECO_UNCLEAR, S.HIGH, _),
    ("N-S4 dilution faible", NANO, "N-S4", {"new_shares": 3e6}, DEFAULT_PRICE, "N-S4", A.RECO_HOLD, S.INFO, _),
    ("N-N3 secteur", NANO, "N-N3", {}, DEFAULT_PRICE, "N-N3", A.IGNORE, S.INFO, _),
]


@pytest.mark.parametrize("cfg, rule_id, figures, price, shown_id, action, severity, arm_id",
                         [case[1:] for case in OVERRIDE_PATHS], ids=[case[0] for case in OVERRIDE_PATHS])
def test_override_paths(defaults: Defaults, cfg, rule_id, figures, price, shown_id, action, severity,
                        arm_id) -> None:
    r = one(cfg, defaults, rule_match(rule_id, **figures), price=price)
    assert (r.alert.rule_id, r.alert.action, r.alert.severity) == (shown_id, action, severity)
    assert r.alert.source_rule_id == rule_id
    assert (r.arm.arm.id if r.arm else None) == arm_id
    assert r.alert.is_rumor is False


def test_alert_content(defaults: Defaults) -> None:
    r = one(UBI, defaults, rule_match("U-S1", ["a", "a", "b"], new_shares=40e6))
    a = r.alert
    assert (a.agent_id, a.origin, a.confidence, a.event_date) == ("UBI", "event", 0.9, LAST_CLOSE_DATE)
    assert [e.item_id for e in a.evidence] == ["a", "b"]           # preuves dédoublonnées
    assert all(e.primary for e in a.evidence)
    assert a.figures == {"new_shares": 40e6}
    assert a.metrics["dilution_pct"] == pytest.approx(100 * 40e6 / (136_237_168 + 40e6), abs=1e-5)
    assert a.price == DEFAULT_PRICE
    assert a.downgrade_reason is None


def test_missing_figures_reason(defaults: Defaults) -> None:
    a = one(UBI, defaults, rule_match("U-S1")).alert
    assert a.downgrade_reason.startswith("chiffres non extraits, lis la source")
    assert "new_shares non extrait" in a.downgrade_reason


def test_armed_reference_value(defaults: Defaults) -> None:
    r = one(UBI, defaults, rule_match("U-B1", offer_price=11.0))
    assert r.arm.ref_value == 11.0
    assert r.alert.metrics == {"figure_vs_prev_close(offer_price)": pytest.approx(11.0 / 9.5, abs=1e-6)}


# --------------------------------------------------------------------------- garde-fou actionnable


def test_low_confidence_downgrades_actionable(defaults: Defaults) -> None:
    a = one(UBI, defaults, rule_match("U-S2", confidence=0.7)).alert
    assert (a.action, a.severity) == (A.RECO_UNCLEAR, S.CRITICAL)
    assert a.downgrade_reason == "RECO_SELL_ALL rétrogradé en RECO_UNCLEAR : confiance 0,70 < 0,80"


def test_confidence_at_threshold_is_accepted(defaults: Defaults) -> None:
    assert one(UBI, defaults, rule_match("U-S2", confidence=0.8)).alert.action is A.RECO_SELL_ALL


def test_low_confidence_does_not_touch_non_actionable(defaults: Defaults) -> None:
    a = one(UBI, defaults, rule_match("U-S5", confidence=0.3)).alert
    assert a.action is A.RECO_UNCLEAR and a.downgrade_reason is None
    assert one(UBI, defaults, rule_match("U-N1", confidence=0.3)).alert.action is A.RECO_HOLD


def test_buy_on_rumor_is_downgraded(defaults: Defaults) -> None:
    a = one(UBI_WATCH, defaults, rule_match("U-E1", ["p"], new_shares=0)).alert
    assert (a.action, a.severity, a.is_rumor) == (A.RECO_UNCLEAR, S.INFO, True)
    assert "aucune source primaire" in a.downgrade_reason


# --------------------------------------------------------------------------- rumeurs


def test_rumor_is_info_and_never_actionable(defaults: Defaults) -> None:
    a = one(UBI, defaults, rule_match("U-S2", ["p", "q"])).alert
    assert (a.action, a.severity, a.is_rumor) == (A.RECO_UNCLEAR, S.INFO, True)
    assert all(not e.primary for e in a.evidence)


@pytest.mark.parametrize("last, prev, severity", [
    (11.2, 10.0, S.HIGH),     # +12 %
    (9.0, 10.0, S.HIGH),      # -10 % exactement (-9,999999999999998 en flottant)
    (10.9, 10.0, S.INFO),     # +9 %
])
def test_rumor_promoted_on_large_move(defaults: Defaults, last: float, prev: float, severity: Severity) -> None:
    a = one(UBI, defaults, rule_match("U-S2", ["p"]), price=snapshot(last, prev)).alert
    assert a.severity is severity


def test_rumor_without_price_stays_info(defaults: Defaults) -> None:
    assert one(UBI, defaults, rule_match("U-S2", ["p"]), price=None).alert.severity is S.INFO


def test_rumor_is_never_critical_and_never_arms(defaults: Defaults) -> None:
    r = one(UBI, defaults, rule_match("U-B1", ["p"], offer_price=11.0), price=snapshot(11.5, 9.5))
    assert (r.alert.action, r.alert.severity) == (A.RECO_HOLD, S.HIGH)   # CRITICAL en primaire
    assert r.arm is None
    assert r.alert.downgrade_reason == "surveillance U-B1-EXIT non armée : aucune source primaire (rumeur)"


def test_one_primary_evidence_is_enough(defaults: Defaults) -> None:
    r = one(UBI, defaults, rule_match("U-B1", ["p", "a"], offer_price=11.0))
    assert r.alert.is_rumor is False and r.alert.severity is S.CRITICAL
    assert r.arm is not None


# --------------------------------------------------------------------------- armement


def test_arm_requires_confidence_for_actionable_exit(defaults: Defaults) -> None:
    r = one(UBI, defaults, rule_match("U-B1", confidence=0.6, offer_price=11.0))
    assert r.alert.action is A.RECO_HOLD      # l'override lui-même n'est pas actionnable
    assert r.arm is None
    assert r.alert.downgrade_reason == "surveillance U-B1-EXIT non armée : confiance 0,60 < 0,80"


def test_no_arm_on_discount_offer(defaults: Defaults) -> None:
    assert one(UBI, defaults, rule_match("U-B1", offer_price=9.0)).arm is None


# --------------------------------------------------------------------------- matches ignorés


def test_stale_match_is_ignored(defaults: Defaults) -> None:
    max_age = defaults.ingestion.max_event_age_days
    assert resolve(UBI, defaults, rule_match("U-S2", event_date=TODAY - timedelta(days=max_age + 1))) == []
    assert len(resolve(UBI, defaults, rule_match("U-S2", event_date=TODAY - timedelta(days=max_age)))) == 1


def test_inactive_or_unknown_rule_is_ignored(defaults: Defaults, caplog) -> None:
    assert resolve(UBI, defaults, rule_match("U-E1", new_shares=0)) == []        # phase WATCH, agent OWNED
    assert resolve(UBI, defaults, rule_match("U-ZZ")) == []
    assert resolve(NANO, defaults, rule_match("N-S4", new_shares=1e6), fired={"N-B1"}) == []   # unless_fired
    assert "inactive ou inconnue" in caplog.text


def test_unknown_items(defaults: Defaults) -> None:
    assert resolve(UBI, defaults, rule_match("U-S2", ["zz"])) == []
    r = one(UBI, defaults, rule_match("U-S2", ["zz", "a"]))
    assert [e.item_id for e in r.alert.evidence] == ["a"]


# --------------------------------------------------------------------------- contradictions


def test_opposite_directions_on_same_document(defaults: Defaults) -> None:
    b1, s1 = resolve(NANO, defaults, rule_match("N-B1", ["a"]), rule_match("N-S1", ["a", "b"]),
                     price=snapshot(50.0, 40.0))
    for r, other in ((b1, "N-S1"), (s1, "N-B1")):
        assert r.alert.action is A.RECO_UNCLEAR
        assert r.alert.downgrade_reason == (
            f"rétrogradé en RECO_UNCLEAR : signaux contradictoires sur le même document (avec {other})")
    assert b1.alert.severity is S.CRITICAL


def test_no_contradiction_on_distinct_documents_or_neutral(defaults: Defaults) -> None:
    resolved = resolve(NANO, defaults, rule_match("N-B1", ["a"]), rule_match("N-S1", ["b"]),
                       rule_match("N-N1", ["b"]))
    assert [r.alert.action for r in resolved] == [A.RECO_UNCLEAR, A.RECO_SELL_ALL, A.RECO_HOLD]
    assert all(r.alert.downgrade_reason is None for r in resolved)


def test_contradiction_cancels_arm(defaults: Defaults) -> None:
    b1, s2 = resolve(UBI, defaults, rule_match("U-B1", offer_price=11.0), rule_match("U-S2"))
    assert b1.arm is None and s2.alert.action is A.RECO_UNCLEAR
    assert "surveillance U-B1-EXIT non armée : signaux contradictoires" in b1.alert.downgrade_reason


def _with_rule(cfg: AgentConfig, rule: dict) -> AgentConfig:
    return cfg.model_copy(update={"rules": [*cfg.rules, EventRule.model_validate(rule)]})


CUSTOM_ARMED = {
    "id": "X-B1", "kind": "event", "phase": "OWNED", "direction": "bullish", "trigger": "t",
    "action": "RECO_HOLD", "severity": "INFO",
    "figures": {"offer_price": "prix", "exit_price": "prix de sortie"},
    "overrides": [{
        "when": {"metric": "figure", "figure": "offer_price", "op": ">", "value": 0},
        "action": "RECO_BUY", "severity": "HIGH",
        "arms": {"id": "X-B1-EXIT", "when": {"metric": "price_vs_figure", "figure": "exit_price", "op": ">=",
                                              "value": 1.0},
                 "action": "RECO_HOLD", "severity": "HIGH"},
    }],
}


def test_downgraded_override_does_not_arm(defaults: Defaults) -> None:
    cfg = _with_rule(UBI, CUSTOM_ARMED)
    r = one(cfg, defaults, rule_match("X-B1", confidence=0.5, offer_price=11.0, exit_price=12.0))
    assert r.alert.action is A.RECO_UNCLEAR and r.arm is None
    assert "surveillance X-B1-EXIT non armée : recommandation rétrogradée" in r.alert.downgrade_reason


@pytest.mark.parametrize("figures, reason", [
    ({"offer_price": 11.0}, "chiffre exit_price non extrait"),
    ({"offer_price": 11.0, "exit_price": 0}, "chiffre exit_price invalide (0)"),
])
def test_arm_needs_its_reference_figure(defaults: Defaults, figures: dict, reason: str) -> None:
    r = one(_with_rule(UBI, CUSTOM_ARMED), defaults, rule_match("X-B1", **figures))
    assert r.alert.action is A.RECO_BUY and r.arm is None
    assert r.alert.downgrade_reason == f"surveillance X-B1-EXIT non armée : {reason}"
