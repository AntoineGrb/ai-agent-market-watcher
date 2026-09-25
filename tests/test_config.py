from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from watcher.config import (
    Action,
    AgentConfig,
    ConfigError,
    Defaults,
    EventRule,
    TimeRule,
    load_agent,
    load_agents,
    load_defaults,
)
from tests.conftest import REPO_AGENTS_DIR


def _rule(rules: list[dict], rule_id: str) -> dict:
    return next(r for r in rules if r["id"] == rule_id)


# --------------------------------------------------------------------------- configs réelles du dépôt


def test_repo_defaults_load() -> None:
    defaults = load_defaults(REPO_AGENTS_DIR)
    assert defaults.rumor.promote_if_abs_move_pct == 10
    assert defaults.price_anomaly.abs_move_pct == 20
    assert defaults.schedule.tz.key == "Europe/Paris"
    assert Action.RECO_SELL_ALL in defaults.guardrails.actionable_actions


def test_repo_agents_load() -> None:
    result = load_agents(REPO_AGENTS_DIR)
    assert result.errors == {}
    assert set(result.configs) == {"NANO", "UBI"}
    nano = result.configs["NANO"]
    assert nano.position.isin == "FR0011341205"
    assert {s.name for s in nano.sources if not s.enabled} == {"Johnson & Johnson communiqués", "Nanobiotix IR"}


def test_repo_ubi_rules_by_phase() -> None:
    ubi = load_agents(REPO_AGENTS_DIR).configs["UBI"]
    ids = {r.id for r in ubi.active_rules(fired=set())}
    assert "U-E1" not in ids                       # règle WATCH, position OWNED
    assert {"U-B1", "U-S1", "U-N1", "U-T2"} <= ids


# --------------------------------------------------------------------------- isolation par agent


def test_owned_without_entry_price_disables_only_that_agent(base_dir: Path, edit_agent) -> None:
    edit_agent("nano", lambda d: d["position"].update(entry_price=None))
    result = load_agents(base_dir / "agents")
    assert set(result.configs) == {"UBI"}
    assert "status OWNED sans entry_price / entry_date" in result.errors["NANO"].message


def test_invalid_yaml_disables_only_that_agent(base_dir: Path) -> None:
    (base_dir / "agents" / "ubi" / "config.yaml").write_text("agent_id: [UBI\n", encoding="utf-8")
    result = load_agents(base_dir / "agents")
    assert set(result.configs) == {"NANO"}
    assert "YAML invalide" in result.errors["UBI"].message


def test_missing_config_file(base_dir: Path) -> None:
    (base_dir / "agents" / "empty").mkdir()
    result = load_agents(base_dir / "agents")
    assert "fichier absent" in result.errors["EMPTY"].message
    assert set(result.configs) == {"NANO", "UBI"}


def test_reserved_directories_are_not_agents(base_dir: Path) -> None:
    (base_dir / "agents" / "_archive").mkdir()
    (base_dir / "agents" / ".hidden").mkdir()
    assert set(load_agents(base_dir / "agents").configs) == {"NANO", "UBI"}


def test_only_filters_and_unknown_agent_raises(base_dir: Path) -> None:
    assert set(load_agents(base_dir / "agents", only="nano").configs) == {"NANO"}
    with pytest.raises(ConfigError, match="agent inconnu"):
        load_agents(base_dir / "agents", only="TTWO")


def test_agent_id_must_match_directory(base_dir: Path, edit_agent) -> None:
    edit_agent("nano", lambda d: d.update(agent_id="NANOB"))
    assert "différent du nom du dossier" in load_agents(base_dir / "agents").errors["NANO"].message


def test_fingerprint_is_stable(base_dir: Path, edit_agent) -> None:
    edit_agent("nano", lambda d: d["position"].update(entry_price=None))
    first = load_agents(base_dir / "agents").errors["NANO"]
    second = load_agents(base_dir / "agents").errors["NANO"]
    assert first.fingerprint == second.fingerprint
    assert "errors.pydantic.dev" not in first.message


def test_source_validator_errors_become_config_errors(base_dir: Path) -> None:
    def validator(source):
        if source.type == "dila_amf":
            raise ValueError("isin manquant")

    with pytest.raises(ConfigError, match="source 'AMF informations réglementées' .* isin manquant"):
        load_agent(base_dir / "agents" / "ubi", validator)


def test_disabled_sources_are_not_validated(base_dir: Path) -> None:
    seen = []
    load_agent(base_dir / "agents" / "nano", lambda s: seen.append(s.name))
    assert "Nanobiotix IR" not in seen and "SEC EDGAR" in seen


# --------------------------------------------------------------------------- règles de validation du schéma


def _nano_data() -> dict:
    import yaml

    return yaml.safe_load((REPO_AGENTS_DIR / "nano" / "config.yaml").read_text(encoding="utf-8"))


def test_duplicate_rule_ids() -> None:
    data = _nano_data()
    data["rules"].append(dict(_rule(data["rules"], "N-S1")))
    with pytest.raises(ValidationError, match="IDs de règles dupliqués"):
        AgentConfig.model_validate(data)


def test_unknown_unless_fired() -> None:
    data = _nano_data()
    _rule(data["rules"], "N-S4")["unless_fired"] = ["N-B1", "N-ZZ"]
    with pytest.raises(ValidationError, match=r"unless_fired référence des règles inconnues : \['N-ZZ'\]"):
        AgentConfig.model_validate(data)


def test_duplicate_source_names() -> None:
    data = _nano_data()
    data["sources"].append(dict(data["sources"][0]))
    with pytest.raises(ValidationError, match="noms de sources dupliqués"):
        AgentConfig.model_validate(data)


def test_unknown_key_is_rejected() -> None:
    data = _nano_data()
    _rule(data["rules"], "N-S4")["unles_fired"] = ["N-B1"]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentConfig.model_validate(data)


def test_invalid_isin() -> None:
    data = _nano_data()
    data["position"]["isin"] = "FR001134120"
    with pytest.raises(ValidationError, match="isin"):
        AgentConfig.model_validate(data)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"deadline": "2027-01-01", "max_holding_days": 365}, "exactement un de deadline / max_holding_days"),
        ({"deadline": None}, "exactement un de deadline / max_holding_days"),
        ({"deadline": None, "max_holding_days": 365, "phase": "WATCH"}, "n'a de sens qu'en phase OWNED"),
        ({"deadline": None, "max_holding_days": 0}, "greater than 0"),
    ],
)
def test_time_rule_invalid(patch: dict, message: str) -> None:
    base = {"id": "X-T1", "kind": "time", "phase": "OWNED", "direction": "bearish",
            "action": "RECO_SELL_ALL", "severity": "HIGH", "deadline": "2027-01-01"}
    with pytest.raises(ValidationError, match=message):
        TimeRule.model_validate({**base, **patch})


def test_figure_metric_requires_figure() -> None:
    data = _nano_data()
    _rule(data["rules"], "N-B3")["overrides"][1]["when"].pop("figure")
    with pytest.raises(ValidationError, match="metric figure_vs_prev_close requiert 'figure'"):
        AgentConfig.model_validate(data)


def test_referenced_figure_must_be_declared() -> None:
    data = _nano_data()
    _rule(data["rules"], "N-B3")["figures"] = {}
    with pytest.raises(ValidationError, match=r"chiffres référencés mais absents de 'figures' : \['offer_price'\]"):
        AgentConfig.model_validate(data)


def test_override_defaults() -> None:
    rule = EventRule.model_validate({
        "id": "X-S1", "kind": "event", "phase": "OWNED", "direction": "bearish", "trigger": "t",
        "action": "RECO_HOLD", "severity": "INFO", "figures": {"new_shares": "d"},
        "overrides": [{"when": {"metric": "dilution_pct", "op": ">", "value": 20},
                       "action": "RECO_UNCLEAR", "severity": "HIGH"}],
    })
    assert rule.overrides[0].if_unavailable == "unclear"
    assert rule.overrides[0].id is None


# --------------------------------------------------------------------------- règles actives


def test_active_rules_unless_fired_and_closed() -> None:
    cfg = AgentConfig.model_validate(_nano_data())
    all_ids = {r.id for r in cfg.active_rules(set())}
    after_readout = {r.id for r in cfg.active_rules({"N-B1"})}
    assert all_ids - after_readout == {"N-S4", "N-T1"}
    assert {r.id for r in cfg.event_rules(set())} >= {"N-B1", "N-S1", "N-N3"}
    assert all(isinstance(r, EventRule) for r in cfg.event_rules(set()))

    data = _nano_data()
    data["position"]["status"] = "CLOSED"
    assert AgentConfig.model_validate(data).active_rules(set()) == []


def test_watch_phase_rules() -> None:
    data = _nano_data()
    data["position"].update(status="WATCH", entry_price=None, entry_date=None)
    ids = {r.id for r in AgentConfig.model_validate(data).active_rules(set())}
    assert ids == {"N-N1", "N-N2", "N-N3"}


# --------------------------------------------------------------------------- defaults


def _defaults_data() -> dict:
    import yaml

    return yaml.safe_load((REPO_AGENTS_DIR / "_defaults.yaml").read_text(encoding="utf-8"))


def test_defaults_priority_must_be_complete() -> None:
    data = _defaults_data()
    data["priority"].remove("IGNORE")
    with pytest.raises(ValidationError, match="priority doit lister chaque action"):
        Defaults.model_validate(data)


def test_defaults_rumor_never_critical() -> None:
    data = _defaults_data()
    data["rumor"]["promoted_severity"] = "CRITICAL"
    with pytest.raises(ValidationError, match="jamais CRITICAL"):
        Defaults.model_validate(data)


def test_defaults_unknown_timezone() -> None:
    data = _defaults_data()
    data["schedule"]["timezone"] = "Europe/Paaris"
    with pytest.raises(ValidationError, match="fuseau horaire inconnu"):
        Defaults.model_validate(data)


def test_load_defaults_invalid_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / "_defaults.yaml").write_text("models: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="_defaults.yaml invalide"):
        load_defaults(tmp_path)


def test_missing_agents_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dossier des agents introuvable"):
        load_agents(tmp_path / "nope")
