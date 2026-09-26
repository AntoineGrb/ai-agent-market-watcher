from __future__ import annotations

from pathlib import Path

import pytest

from watcher.config import ConfigError
from watcher.fixtures import FIXTURE_SOURCE_TYPE, discover_fixtures, load_fixture
from watcher.llm.evals import eval_config, format_report, sale_rule_ids, score_case, sell_all_rule_ids
from watcher.settings import PROJECT_ROOT
from tests.conftest import NOW, repo_agent, rule_match

REPO_FIXTURES = discover_fixtures(PROJECT_ROOT / "fixtures")


def _write(tmp_path: Path, content: str, name: str = "cas.md") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


HEADER = "---\nagent: nano\nsource_name: SEC EDGAR\nsource_primary: true\nsynthetic: true\n"


# --------------------------------------------------------------------------- chargement


def test_load_fixture_and_build_item(tmp_path: Path) -> None:
    fx = load_fixture(_write(tmp_path, HEADER + "expected_rule_ids: [N-S1]\n---\n\n# Titre du communiqué\n\nCorps.\n"))
    assert fx.name == "NANO/cas" and fx.title == "Titre du communiqué"
    item = fx.to_item(now=NOW)
    assert item.agent_id == "NANO" and item.source_primary and item.source_type == FIXTURE_SOURCE_TYPE
    assert item.published_at == NOW and item.text == "# Titre du communiqué\n\nCorps."
    assert str(item.url) == "https://fixtures.invalid/nano/cas"
    assert fx.to_item(now=NOW).id == item.id                       # ID stable
    assert fx.to_item(now=NOW, primary=False).source_primary is False


@pytest.mark.parametrize("content, message", [
    ("Pas d'en-tête\n", "en-tête YAML absent"),
    (HEADER + "Corps sans fermeture\n", "non fermé"),
    (HEADER + "inconnu: 1\n---\nCorps\n", "inconnu"),
    (HEADER + "---\n\n", "texte du document vide"),
    ("---\nagent: [\n---\nCorps\n", "YAML invalide"),
])
def test_invalid_fixtures(tmp_path: Path, content: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_fixture(_write(tmp_path, content))


def test_missing_fixture(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="introuvable"):
        load_fixture(tmp_path / "absent.md")


# --------------------------------------------------------------------------- jeu du dépôt


def test_repo_fixture_set_covers_both_agents() -> None:
    by_agent = {a: [f for f in REPO_FIXTURES if f.header.agent == a] for a in ("NANO", "UBI")}
    assert all(len(fixtures) >= 10 for fixtures in by_agent.values())
    assert any(not f.header.synthetic for f in by_agent["NANO"])   # au moins un vrai communiqué


@pytest.mark.parametrize("fixture", REPO_FIXTURES, ids=lambda f: f.name)
def test_repo_fixture_is_consistent_with_config(fixture) -> None:
    """Règles attendues actives dans la phase évaluée, chiffres attendus déclarés par ces règles."""
    cfg = eval_config(repo_agent(fixture.header.agent.lower()), fixture)
    rules = {r.id: r for r in cfg.event_rules(set())}
    referenced = set(fixture.header.expected_rule_ids) | set(fixture.header.allowed_rule_ids)
    assert referenced <= rules.keys(), f"règles inconnues ou inactives : {referenced - rules.keys()}"
    declared = {name for rid in fixture.header.expected_rule_ids for name in rules[rid].figures}
    assert fixture.header.expected_figures.keys() <= declared
    assert cfg.prompt


# --------------------------------------------------------------------------- notation


def _fixture(tmp_path: Path, extra: str, agent: str = "nano", primary: bool = True):
    header = HEADER.replace("nano", agent).replace("true\nsynthetic", f"{str(primary).lower()}\nsynthetic")
    return load_fixture(_write(tmp_path, header + extra + "---\nCorps\n"))


def test_sale_rules() -> None:
    rules = repo_agent("ubi").event_rules(set())
    assert sale_rule_ids(rules) == {"U-B1", "U-S1", "U-S2", "U-S3"}   # U-B1 : via la surveillance armée
    assert sell_all_rule_ids(repo_agent("nano").event_rules(set())) == {"N-B3", "N-S1", "N-S2", "N-S3"}


def test_score_exact_case(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, "expected_rule_ids: [N-S4]\nexpected_figures: {new_shares: 3000000}\n")
    r = score_case(fx, repo_agent("nano"), [rule_match("N-S4", new_shares=3_000_000)], model="m")
    assert r.exact and not r.critical


def test_score_critical_false_negative(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, "expected_rule_ids: [N-S1]\n")
    r = score_case(fx, repo_agent("nano"), [], model="m")
    assert r.critical and r.critical_false_negative and not r.passed
    rumor = score_case(_fixture(tmp_path, "expected_rule_ids: [N-S1]\n", primary=False), repo_agent("nano"), [],
                       model="m")
    assert not rumor.critical and rumor.passed and rumor.missing == ["N-S1"]


def test_score_sale_false_positive_and_tolerated_matches(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, "expected_rule_ids: []\nallowed_rule_ids: [N-S4]\n")
    r = score_case(fx, repo_agent("nano"), [rule_match("N-S4"), rule_match("N-S3"), rule_match("N-N2")], model="m")
    assert r.sale_false_positives == ["N-S3"] and r.unexpected == ["N-N2", "N-S3"] and not r.passed


def test_score_figures(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, "expected_rule_ids: [N-B3]\nexpected_figures: {offer_price: 38.5}\n")
    cfg = repo_agent("nano")
    assert score_case(fx, cfg, [rule_match("N-B3", offer_price=38.5)], model="m").exact
    wrong = score_case(fx, cfg, [rule_match("N-B3", offer_price=35.0)], model="m")
    assert wrong.figure_errors == ["offer_price = 35 (attendu 38.5)"] and not wrong.passed
    absent = score_case(fx, cfg, [rule_match("N-B3")], model="m")
    assert absent.figure_errors == ["offer_price absent (attendu 38.5)"]
    missed = score_case(fx, cfg, [], model="m")
    assert missed.figure_errors == [] and missed.critical_false_negative


def test_eval_config_simulates_phase(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, "status: WATCH\nexpected_rule_ids: [U-E1]\n", agent="ubi")
    cfg = eval_config(repo_agent("ubi"), fx)
    assert cfg.position.status == "WATCH" and "U-E1" in {r.id for r in cfg.event_rules(set())}
    assert cfg.prompt == repo_agent("ubi").prompt


def test_format_report(tmp_path: Path) -> None:
    ok = score_case(_fixture(tmp_path, "expected_rule_ids: [N-N2]\n"), repo_agent("nano"),
                    [rule_match("N-N2")], model="haiku + sonnet")
    ko = score_case(_fixture(tmp_path, "expected_rule_ids: [N-S1]\n", primary=True), repo_agent("nano"), [],
                    model="haiku + sonnet")
    ko.error = "API indisponible"
    report = format_report([ok, ko])
    assert "| haiku + sonnet | NANO/cas | oui | N-N2 | N-N2 | OK |  |" in report
    assert "ÉCHEC" in report and "FAUX NÉGATIF CRITIQUE" in report and "erreur : API indisponible" in report
    assert report.endswith("1/2 cas conformes aux critères d'acceptation, 1 sans aucun écart.")
