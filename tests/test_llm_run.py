"""Couche LLM branchée dans le run : bout en bout, échecs, budget, `--inject`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from watcher import run as run_module
from watcher.config import ConfigError, load_agent
from watcher.models import NewsItem
from watcher.run import EXIT_OK, EXIT_USAGE, Runner, main
from watcher.settings import Settings
from watcher.sources import base
from watcher.store import Store
from tests.conftest import (
    NOW,
    FakeHealthchecks,
    FakeMailer,
    ScriptedModel,
    analysis_output,
    llm_layer,
    match_output,
    news,
)


class DocsFetcher:
    """Fetcher de test : renvoie des documents fixes pour chaque source de son type."""

    def __init__(self, source_type: str, items: list[NewsItem]) -> None:
        self.source_type = source_type
        self.items = items

    def validate_params(self, params):
        pass

    def fetch(self, source, agent, since, state):
        return [it.model_copy(update={"agent_id": agent.agent_id, "source_name": source.name})
                for it in self.items if it.agent_id == agent.agent_id]

    def fetch_text(self, item):
        return f"Texte complet de {item.title}"


@pytest.fixture
def amf_docs(monkeypatch: pytest.MonkeyPatch) -> list[NewsItem]:
    """Un document primaire par agent, servi par la source AMF (commune aux deux agents)."""
    docs = [news("nano-1", agent_id="NANO"), news("ubi-1", agent_id="UBI")]
    monkeypatch.setattr(base, "_REGISTRY", {"dila_amf": DocsFetcher("dila_amf", docs)})
    return docs


def _runner(settings: Settings, store: Store, llm) -> tuple[Runner, FakeMailer, FakeHealthchecks]:
    mailer, hc = FakeMailer(), FakeHealthchecks()
    return Runner(settings, store, mailer, hc, clock=lambda: NOW, llm=llm), mailer, hc


def _triage_relevant(store: Store, agent_id: str) -> dict[str, int | None]:
    rows = store._conn.execute("SELECT item_id, triage_relevant FROM seen_items WHERE agent_id = ?",
                               (agent_id,)).fetchall()
    return {r["item_id"]: r["triage_relevant"] for r in rows}


def test_end_to_end_primary_sell_signal(settings: Settings, store: Store, amf_docs) -> None:
    triage = ScriptedModel({"relevant_ids": ["D1"]})
    analysis = ScriptedModel(
        # NANO : échec de la phase 3 ; UBI : liste vide (appels dans l'ordre alphabétique des agents).
        analysis_output(match_output("N-S1", "D1", confidence=0.95)), analysis_output(),
    )
    runner, mailer, hc = _runner(settings, store, llm_layer(settings, triage, analysis))
    report = runner.run(now=NOW)

    assert report.status == "ok" and report.alerts_created == 1
    [mail] = mailer.sent
    assert mail.subject.startswith("[CRITICAL] NANO · Vendre toute la ligne")
    assert _triage_relevant(store, "NANO") == {"nano-1": 1}
    assert "Texte complet de Document nano-1" in analysis.prompt(0)
    run_row = store.get_run(report.run_id)
    assert run_row["input_tokens"] > 0 and run_row["output_tokens"] > 0
    assert report.usage.requests == 4                                   # 2 tris + 2 analyses
    usage = store.runs_since(NOW)[0].usage_by_model
    assert sum(t[0] for t in usage.values()) == run_row["input_tokens"]  # détail par modèle (coût du heartbeat)
    assert "Règle : N-S1 · " in mail.body and "[source primaire]" in mail.body
    assert "N-S1" in store.fired_rule_ids("NANO")


def test_analysis_failure_fails_agent_and_keeps_documents_unseen(settings: Settings, store: Store,
                                                                 amf_docs) -> None:
    triage = ScriptedModel({"relevant_ids": ["D1"]})
    analysis = ScriptedModel(analysis_output(match_output("N-S9")))   # rule_id inconnu à chaque tentative
    runner, _, hc = _runner(settings, store, llm_layer(settings, triage, analysis))
    report = runner.run(now=NOW)

    assert report.status == "failed"                                     # les deux agents échouent
    assert "LlmError" in report.errors["NANO"] and "analyse, lot 1/1" in report.errors["NANO"]
    assert store.seen_item_ids("NANO", ["nano-1"]) == set()            # retraité au run suivant
    assert hc.calls[-1][0] == "fail"
    assert store.get_run(report.run_id)["input_tokens"] > 0           # tokens comptés malgré l'échec


def test_budget_exhaustion_fails_the_run(settings: Settings, store: Store, amf_docs) -> None:
    defaults_path = settings.agents_dir / "_defaults.yaml"
    defaults_path.write_text(defaults_path.read_text(encoding="utf-8").replace(
        "max_total_tokens_per_run: 300000", "max_total_tokens_per_run: 10"), encoding="utf-8")
    runner, _, _ = _runner(settings, store, llm_layer(settings))
    report = runner.run(now=NOW)
    assert report.status == "failed"
    assert "budget de 10 tokens" in report.errors["_budget"]
    assert json.loads(store.get_run(report.run_id)["errors_json"])["_budget"]


def test_documents_without_llm_layer_fail_the_agent(settings: Settings, store: Store, amf_docs) -> None:
    runner, _, _ = _runner(settings, store, None)
    report = runner.run(now=NOW, only="UBI")
    assert report.errors["UBI"] == "LlmConfigError : couche LLM non configurée"


def test_missing_prompt_is_a_config_error(settings: Settings) -> None:
    (settings.agents_dir / "ubi" / "prompt.md").unlink()
    with pytest.raises(ConfigError, match="prompt.md"):
        load_agent(settings.agents_dir / "ubi")
    (settings.agents_dir / "ubi" / "prompt.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="prompt.md est vide"):
        load_agent(settings.agents_dir / "ubi")


# --------------------------------------------------------------------------- --inject


FIXTURE = """---
agent: NANO
source_name: Google News EN
source_primary: false
synthetic: true
expected_rule_ids: [N-S1]
---
Nanobiotix: NANORAY-312 did not meet its primary endpoint.
"""


@pytest.fixture
def inject_env(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """`main` isolé : env capturé, LLM simulé, aucun réseau, mails collectés."""
    captured: dict = {"sent": []}

    def from_env(cls, environ=None, **kw):
        captured["env"] = kw.get("env", settings.env)
        return settings.model_copy(update={**kw, "env": "test"})

    monkeypatch.setattr(run_module, "_load_dotenv", lambda: None)
    monkeypatch.setattr(run_module.Settings, "from_env", classmethod(from_env))
    monkeypatch.setattr(run_module, "setup_logging", lambda log_dir: None)
    monkeypatch.setattr(run_module, "_utcnow", lambda: NOW)   # event_date des matches simulés : 24/09
    monkeypatch.setattr(run_module, "register_builtin_fetchers", lambda settings, client: None)
    monkeypatch.setattr(run_module, "build_price_service", lambda client: None)
    monkeypatch.setattr(run_module.Mailer, "send", lambda self, mail: captured["sent"].append(mail))
    triage = ScriptedModel({"relevant_ids": ["D1"]})
    analysis = ScriptedModel(analysis_output(match_output("N-S1", "D1")))
    monkeypatch.setattr(run_module, "LlmLayer", lambda s: llm_layer(s, triage, analysis))
    path = tmp_path / "echec.md"
    path.write_text(FIXTURE, encoding="utf-8")
    captured["path"] = path
    return captured


def test_inject_runs_full_pipeline_in_test_env(inject_env, settings: Settings) -> None:
    assert main(["--agent", "nano", "--inject", str(inject_env["path"])]) == EXIT_OK
    assert inject_env["env"] == "test"
    [mail] = inject_env["sent"]
    # Source non primaire : rumeur, jamais actionnable → rétrogradée en RECO_UNCLEAR, INFO → digest.
    assert mail.subject.startswith("[INFO]")
    with Store.open(settings.db_path) as store:
        assert store.get_run(1)["status"] == "ok"
        assert store.get_run(1)["scope"] == "inject NANO"


def test_inject_primary_flag(inject_env) -> None:
    assert main(["--agent", "NANO", "--inject", str(inject_env["path"]), "--primary"]) == EXIT_OK
    [mail] = inject_env["sent"]
    assert mail.subject.startswith("[CRITICAL] NANO · Vendre toute la ligne")


def test_inject_rejects_other_agent_and_bad_file(inject_env, tmp_path: Path) -> None:
    assert main(["--agent", "UBI", "--inject", str(inject_env["path"])]) == EXIT_USAGE
    assert main(["--agent", "NANO", "--inject", str(tmp_path / "absent.md")]) == EXIT_USAGE
    assert inject_env["sent"] == []


@pytest.mark.parametrize("argv", [
    ["--all", "--inject", "x.md"],
    ["--agent", "NANO", "--inject", "x.md", "--baseline"],
    ["--agent", "NANO", "--primary"],
])
def test_inject_argument_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == EXIT_USAGE
