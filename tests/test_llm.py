from __future__ import annotations

import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.test import TestModel

from watcher.config import Defaults
from watcher.llm import LlmBudgetExceeded, LlmConfigError, LlmError, LlmLayer, TokenBudget
from watcher.llm.analysis import run_analysis
from watcher.llm.instructions import (
    GLOBAL_INSTRUCTIONS,
    analysis_instructions,
    analysis_prompt,
    triage_instructions,
    triage_prompt,
)
from watcher.llm.runtime import assign_refs, resolve_model
from watcher.llm.triage import run_triage
from watcher.models import NewsItem
from watcher.settings import Settings
from tests.conftest import (
    TODAY,
    ScriptedModel,
    analysis_output,
    llm_layer,
    match_output,
    news,
    repo_agent,
    snapshot,
    triage_output,
)

BIG_BUDGET = 1_000_000


def _budget(max_tokens: int = BIG_BUDGET) -> TokenBudget:
    return TokenBudget(max_tokens)


def _items(*keys: str, agent_id: str = "NANO") -> list[NewsItem]:
    return [news(k, agent_id=agent_id).model_copy(update={"text": f"Texte {k}"}) for k in keys]


def _analyze(model: ScriptedModel, items: list[NewsItem], *, fired: set[str] | None = None,
             budget: TokenBudget | None = None, batch_size: int = 10):
    cfg = repo_agent("nano")
    return run_analysis(items, cfg, cfg.event_rules(fired or set()), snapshot(symbol="NANO.PA"), today=TODAY,
                        model=model, budget=budget or _budget(), batch_size=batch_size, max_doc_chars=15000)


# --------------------------------------------------------------------------- consignes


def test_analysis_instructions_contain_global_prompt_and_active_rules_only() -> None:
    cfg = repo_agent("nano")
    text = analysis_instructions(cfg, cfg.event_rules({"N-B1"}))   # N-B1 déclenchée : N-S4 neutralisée
    assert text.startswith(GLOBAL_INSTRUCTIONS)
    assert "choisis N-S5" in text                                  # prompt.md de l'agent
    assert "### N-S1 (baissier)" in text and "### N-B3 (haussier)" in text
    assert "- offer_price : Prix proposé en EUR" in text           # chiffres attendus avec leur description
    assert "N-S4" not in text                                      # règle inactive : jamais montrée au LLM
    assert "N-B2" not in text and "N-T1" not in text               # règles price / time : évaluées par le code
    assert "RECO_" not in text and "CRITICAL" not in text          # ni action ni sévérité


def test_triage_instructions_show_triggers_without_rule_ids() -> None:
    cfg = repo_agent("ubi")
    text = triage_instructions(cfg, cfg.event_rules(set()))
    assert "Ubisoft Entertainment" in text and "En cas de doute, garde le document" in text
    assert "Offre publique (OPA, OPR, OPAS)" in text and "Mots-clés : Ubisoft, Guillemot" in text
    assert "U-B1" not in text


def test_prompts_use_short_refs_truncate_and_give_price_context() -> None:
    long_item = news("x", agent_id="NANO").model_copy(update={"id": "f" * 64, "summary": "a" * 900, "text": "b" * 50})
    refs = assign_refs([long_item])
    triage = triage_prompt(refs)
    assert "[D1] Document x" in triage and "a" * 499 + "…" in triage and "a" * 500 not in triage
    assert long_item.id not in triage

    cfg = repo_agent("nano")
    prompt = analysis_prompt(cfg, refs, snapshot(10.0, 9.5, symbol="NANO.PA"), today=TODAY, max_doc_chars=20)
    assert "Date du jour : 2026-09-25." in prompt
    assert "Dernière clôture : 10 EUR le 2026-09-24 (variation J-1 : +5.26 %)" in prompt
    assert "## [D1] Document x" in prompt and "b" * 19 + "…" in prompt
    assert "Cours indisponible" in analysis_prompt(cfg, refs, None, today=TODAY, max_doc_chars=20)


# --------------------------------------------------------------------------- analyse : validateur


def test_empty_analysis_is_accepted_in_one_request() -> None:
    model = ScriptedModel(analysis_output())
    assert _analyze(model, _items("a")) == []
    assert len(model.calls) == 1


def test_unknown_rule_id_triggers_retry() -> None:
    model = ScriptedModel(analysis_output(match_output("N-S9")), analysis_output(match_output("N-S1")))
    [m] = _analyze(model, _items("a"))
    assert m.rule_id == "N-S1"
    assert len(model.calls) == 2
    assert "rule_id inconnu : N-S9" in model.retry_feedback(1)


def test_inactive_rule_is_unknown_to_the_validator() -> None:
    model = ScriptedModel(analysis_output(match_output("N-S4", new_shares=1.0)), analysis_output())
    assert _analyze(model, _items("a"), fired={"N-S1"}) == []      # N-S4 inactive après un résultat
    assert "rule_id inconnu : N-S4" in model.retry_feedback(1)


def test_unknown_item_id_triggers_retry() -> None:
    model = ScriptedModel(analysis_output(match_output("N-S1", "D1", "D7")), analysis_output(match_output("N-S1")))
    [m] = _analyze(model, _items("a"))
    assert "item_ids inconnus : ['D7']" in model.retry_feedback(1)
    assert m.item_ids == ["a"]


def test_missing_figure_is_never_retried() -> None:
    model = ScriptedModel(analysis_output(match_output("N-S4")))      # new_shares absent du document
    [m] = _analyze(model, _items("a"))
    assert m.extracted_figures == {} and len(model.calls) == 1


def test_retries_exhausted_raise_llm_error() -> None:
    model = ScriptedModel(analysis_output(match_output("N-S9")))
    with pytest.raises(LlmError, match="analyse, lot 1/1"):
        _analyze(model, _items("a"))
    assert len(model.calls) == 3                                     # 1 + retries=2


def test_refs_are_mapped_back_and_undeclared_figures_dropped() -> None:
    model = ScriptedModel(analysis_output(
        match_output("N-S4", "D2", "D1", "D2", new_shares=3e6, existing_shares=1.0),
    ))
    [m] = _analyze(model, _items("a", "b"))
    assert m.item_ids == ["b", "a"]                                   # IDs réels, sans doublon
    assert m.extracted_figures == {"new_shares": 3e6}                # existing_shares non demandé par N-S4


def test_analysis_batches_restart_refs() -> None:
    model = ScriptedModel(analysis_output(), analysis_output(match_output("N-N2", "D1")))
    [m] = _analyze(model, _items("a", "b", "c"), batch_size=2)
    assert len(model.calls) == 2
    assert "[D1] Document c" in model.prompt(1) and "Document a" not in model.prompt(1)
    assert m.item_ids == ["c"]


# --------------------------------------------------------------------------- tri


def _triage(model: ScriptedModel, items: list[NewsItem], batch_size: int = 50) -> set[str]:
    cfg = repo_agent("nano")
    return run_triage(items, cfg, cfg.event_rules(set()), model=model, budget=_budget(), batch_size=batch_size)


def test_triage_returns_real_ids_per_batch() -> None:
    model = ScriptedModel(triage_output("D2"), triage_output("D1"))
    assert _triage(model, _items("a", "b", "c"), batch_size=2) == {"b", "c"}
    assert len(model.calls) == 2


def test_triage_unknown_ref_triggers_retry() -> None:
    model = ScriptedModel(triage_output("D1", "D3"), triage_output("D1"))
    assert _triage(model, _items("a")) == {"a"}
    assert "références inconnues : ['D3']" in model.retry_feedback(1)


# --------------------------------------------------------------------------- budget et modèles


def test_budget_counts_tokens_even_on_failure() -> None:
    budget = _budget()
    with pytest.raises(LlmError):
        _analyze(ScriptedModel(analysis_output(match_output("N-S9"))), _items("a"), budget=budget)
    assert budget.used.requests == 3 and budget.used.total_tokens > 0


def test_budget_exceeded_during_call() -> None:
    with pytest.raises(LlmBudgetExceeded, match="épuisé pendant"):
        _analyze(ScriptedModel(analysis_output()), _items("a"), budget=_budget(10))


def test_exhausted_budget_blocks_next_call() -> None:
    budget = _budget(10)
    budget.used.input_tokens = 10
    model = ScriptedModel(analysis_output())
    with pytest.raises(LlmBudgetExceeded, match="épuisé avant"):
        _analyze(model, _items("a"), budget=budget)
    assert model.calls == []


def test_resolve_model() -> None:
    with pytest.raises(LlmConfigError, match="ANTHROPIC_API_KEY"):
        resolve_model("anthropic:claude-sonnet-5", Settings(), max_tokens=100)
    model = resolve_model("anthropic:claude-sonnet-5", Settings(anthropic_api_key="sk-test"), max_tokens=100)
    assert isinstance(model, AnthropicModel) and model.model_name == "claude-sonnet-5"
    assert model.settings == {"max_tokens": 100}
    assert isinstance(resolve_model("test", Settings(), max_tokens=100), TestModel)
    with pytest.raises(LlmConfigError):
        resolve_model("fournisseur-inconnu:modele", Settings(), max_tokens=100)


def test_model_names_env_override(defaults: Defaults) -> None:
    layer = LlmLayer(Settings(model_analysis="anthropic:claude-opus-5"))
    assert layer.model_names(defaults) == {"triage": "anthropic:claude-haiku-4-5",
                                           "analysis": "anthropic:claude-opus-5"}


# --------------------------------------------------------------------------- couche complète


def test_process_fetches_text_only_for_retained_documents(defaults: Defaults) -> None:
    fetched: list[str] = []

    def fetch_text(item: NewsItem) -> str:
        fetched.append(item.id)
        if item.id == "c":
            raise RuntimeError("PDF illisible")
        return f"Communiqué complet {item.id}"

    items = [news(k, agent_id="NANO") for k in ("a", "b", "c")]
    items[1] = items[1].model_copy(update={"text": "déjà complet"})
    analysis = ScriptedModel(analysis_output(match_output("N-S1", "D1")))
    layer = llm_layer(Settings(), ScriptedModel(triage_output("D2", "D3")), analysis, text_fetcher=fetch_text)

    out = layer.process(repo_agent("nano"), defaults, items, None, today=TODAY, fired=set(), budget=_budget())
    assert out.relevant == {"b", "c"}
    assert fetched == ["c"]                                           # "a" écarté, "b" déjà complet
    assert out.items["c"].text.endswith("(texte complet indisponible)")
    assert "PDF illisible" in out.warnings[0]
    assert "déjà complet" in analysis.prompt() and "[D2] Document c" in analysis.prompt()
    assert [m.item_ids for m in out.matches] == [["b"]]


def test_process_skips_llm_without_documents_or_rules(defaults: Defaults) -> None:
    triage = ScriptedModel(triage_output("D1"))
    layer = llm_layer(Settings(), triage)
    cfg = repo_agent("nano")
    assert layer.process(cfg, defaults, [], None, today=TODAY, fired=set(), budget=_budget()).relevant == set()
    closed = repo_agent("nano", status="CLOSED")
    out = layer.process(closed, defaults, _items("a"), None, today=TODAY, fired=set(), budget=_budget())
    assert out.relevant == set() and triage.calls == []


def test_process_without_retained_document_skips_analysis(defaults: Defaults) -> None:
    analysis = ScriptedModel(analysis_output())
    layer = llm_layer(Settings(), ScriptedModel(triage_output()), analysis)
    out = layer.process(repo_agent("nano"), defaults, _items("a"), None, today=TODAY, fired=set(),
                        budget=_budget())
    assert out.matches == [] and analysis.calls == []


def test_models_are_resolved_lazily_and_cached(defaults: Defaults) -> None:
    layer = LlmLayer(Settings(anthropic_api_key="sk-test"))
    triage = layer._model("triage", defaults)
    assert isinstance(triage, AnthropicModel) and triage.model_name == "claude-haiku-4-5"
    assert triage.settings == {"max_tokens": 2048}
    assert layer._model("triage", defaults) is triage
    assert layer._model("analysis", defaults).settings == {"max_tokens": 16000}


def test_fetch_text_from_source_uses_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from watcher.llm import fetch_text_from_source
    from watcher.sources import base

    class Fetcher:
        source_type = "dila_amf"

        def fetch_text(self, item: NewsItem) -> str:
            return f"texte {item.id}"

    monkeypatch.setattr(base, "_REGISTRY", {"dila_amf": Fetcher()})
    assert fetch_text_from_source(news("a")) == "texte a"
    with pytest.raises(LookupError):
        fetch_text_from_source(news("b", primary=False))           # google_news_rss non enregistré
