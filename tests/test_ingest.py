from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from watcher.config import Defaults
from watcher.ingest import AgentInputs, fetch_sources, gather
from watcher.models import NewsItem
from watcher.prices import PRICE_STATE_KEY, PriceError, PriceFetch, PriceService
from watcher.run import Runner
from watcher.settings import Settings
from watcher.sources import base
from watcher.store import Store
from tests.conftest import NOW, FakeHealthchecks, FakeMailer, repo_agent

COMPLETE = [(date(2026, 9, 22), 5.00), (date(2026, 9, 23), 5.00), (date(2026, 9, 24), 6.50)]   # +30 % J-1


class FakeFetcher:
    """Fetcher de test : documents fixés, erreur optionnelle, état incrémenté à chaque fetch."""

    def __init__(self, source_type: str, items: list[NewsItem] | None = None, error: Exception | None = None) -> None:
        self.source_type = source_type
        self.items = items or []
        self.error = error
        self.calls = 0

    def validate_params(self, params):
        pass

    def fetch(self, source, agent, since, state):
        self.calls += 1
        if self.error is not None:
            raise self.error
        state.updated = {"runs": (state.previous or {}).get("runs", 0) + 1}
        return [it.model_copy(update={"agent_id": agent.agent_id, "source_name": source.name}) for it in self.items]

    def fetch_text(self, item):
        return ""


def _item(key: str, age_days: float = 0.5, published: bool = True) -> NewsItem:
    return NewsItem(id=key, agent_id="X", source_name="X", source_type="dila_amf", source_primary=True,
                    url=f"https://example.com/{key}", title=f"Doc {key}",
                    published_at=NOW - timedelta(days=age_days) if published else None)


class StubPrices(PriceService):
    def __init__(self, closes=COMPLETE, error: Exception | None = None) -> None:
        self._closes, self._error = closes, error

    def closes(self, position, *, today, sessions=15):
        if self._error is not None:
            raise self._error
        return PriceFetch(list(self._closes), "stub", ("repli stub utilisé",))


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict:
    reg: dict = {}
    monkeypatch.setattr(base, "_REGISTRY", reg)
    return reg


def _register(registry: dict, *fetchers: FakeFetcher) -> None:
    for f in fetchers:
        registry[f.source_type] = f


# --------------------------------------------------------------------------- ingestion


def test_filters_age_duplicates_and_seen(store: Store, defaults: Defaults, registry) -> None:
    amf = FakeFetcher("dila_amf", [_item("new"), _item("old", age_days=10), _item("undated", published=False),
                                   _item("seen")])
    edgar = FakeFetcher("edgar", [_item("new")])        # même ID qu'un document AMF : gardé une fois
    _register(registry, amf, edgar)
    store.mark_seen([_item("seen").model_copy(update={"agent_id": "NANO"})], NOW)

    inputs = gather(store, repo_agent("nano"), defaults, None, now=NOW)

    assert sorted(it.id for it in inputs.items) == ["new", "undated"]
    assert all(it.agent_id == "NANO" for it in inputs.items)
    # Pas de fetcher pour clinicaltrials ni google_news_rss dans ce registre : sources sautées, signalées.
    assert any("clinicaltrials non implémenté" in w for w in inputs.warnings)


def test_source_error_is_a_warning_and_keeps_previous_state(store: Store, defaults: Defaults, registry) -> None:
    ok = FakeFetcher("dila_amf", [_item("a")])
    broken = FakeFetcher("edgar", error=RuntimeError("SEC en panne"))
    _register(registry, ok, broken)
    store.set_source_state("NANO", "SEC EDGAR", {"runs": 7}, NOW)

    inputs = gather(store, repo_agent("nano"), defaults, None, now=NOW)

    assert [it.id for it in inputs.items] == ["a"]
    assert any("SEC EDGAR en erreur : RuntimeError : SEC en panne" in w for w in inputs.warnings)
    assert store.get_source_state("NANO", "SEC EDGAR") == {"runs": 7}
    assert store.get_source_state("NANO", "AMF informations réglementées") == {"runs": 1}


def test_disabled_sources_are_not_fetched(store: Store, defaults: Defaults, registry) -> None:
    rss = FakeFetcher("rss", [_item("jnj")])
    _register(registry, rss)
    inputs = AgentInputs()
    fetch_sources(store, repo_agent("nano"), defaults, now=NOW, inputs=inputs)
    assert rss.calls == 0 and inputs.items == []       # sources J&J et Nanobiotix IR en enabled: false


def test_price_snapshot_and_last_processed_close(store: Store, defaults: Defaults, registry) -> None:
    cfg = repo_agent("ubi")
    first = gather(store, cfg, defaults, StubPrices(), now=NOW)
    assert first.price is not None and first.price.is_new_close
    assert first.price.daily_move_pct == pytest.approx(30.0)
    assert "cours : repli stub utilisé" in first.warnings
    assert store.get_source_state("UBI", PRICE_STATE_KEY)["last_close_date"] == "2026-09-24"

    second = gather(store, cfg, defaults, StubPrices(), now=NOW + timedelta(days=1))   # samedi : même clôture
    assert second.price is not None and not second.price.is_new_close


def test_price_failure_is_a_warning(store: Store, defaults: Defaults, registry) -> None:
    inputs = gather(store, repo_agent("ubi"), defaults, StubPrices(error=PriceError("yfinance KO")), now=NOW)
    assert inputs.price is None
    assert "cours indisponible : yfinance KO" in inputs.warnings
    assert store.get_source_state("UBI", PRICE_STATE_KEY) is None


# --------------------------------------------------------------------------- run complet


def _runner(settings: Settings, store: Store, prices: PriceService | None = None) -> tuple[Runner, FakeMailer]:
    mailer = FakeMailer()
    return Runner(settings, store, mailer, FakeHealthchecks(), clock=lambda: NOW, prices=prices), mailer


def test_baseline_marks_everything_seen_without_alert(settings: Settings, store: Store, registry) -> None:
    _register(registry, FakeFetcher("dila_amf", [_item("a"), _item("b")]), FakeFetcher("clinicaltrials"))
    runner, mailer = _runner(settings, store, StubPrices())

    report = runner.run(now=NOW, baseline=True)

    assert report.status == "ok" and mailer.sent == [] and store.pending_events() == []
    assert store.seen_item_ids("UBI", ["a", "b"]) == {"a", "b"}
    assert store.seen_item_ids("NANO", ["a", "b"]) == {"a", "b"}
    assert store.get_source_state("NANO", "ClinicalTrials.gov") == {"runs": 1}     # instantané initialisé
    assert store.get_source_state("UBI", PRICE_STATE_KEY)["last_close_date"] == "2026-09-24"
    # +30 % J-1 : aucune anomalie en baseline, et la clôture est marquée traitée pour le run suivant.
    runner.run(now=NOW)
    assert store.pending_events() == []


def test_normal_run_uses_price_and_leaves_documents_unseen(settings: Settings, store: Store, registry) -> None:
    _register(registry, FakeFetcher("dila_amf", [_item("a")]))
    runner, mailer = _runner(settings, store, StubPrices())

    report = runner.run(now=NOW)

    assert report.status == "ok"                                        # un avertissement n'est pas une erreur
    assert store.seen_item_ids("UBI", ["a"]) == set()                  # en attente du tri (étape 4)
    # +30 % J-1 sans actualité : le cours alimente bien les règles déterministes.
    assert "P-ANOMALY" in store.fired_rule_ids("UBI") and "P-ANOMALY" in store.fired_rule_ids("NANO")
    assert mailer.sent and store.pending_events() == []
    warnings = json.loads(store.get_run(report.run_id)["errors_json"])["_warnings"]
    assert "cours : repli stub utilisé" in warnings["UBI"]


def test_failed_agent_rolls_back_price_and_source_state(settings: Settings, store: Store, registry,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    _register(registry, FakeFetcher("dila_amf", [_item("a")]))
    runner, _ = _runner(settings, store, StubPrices())
    from watcher import run as run_module

    def boom(*args, **kwargs):
        raise RuntimeError("moteur en panne")

    monkeypatch.setattr(run_module, "evaluate_agent", boom)
    report = runner.run(now=NOW)

    assert sorted(report.agents_failed) == ["NANO", "UBI"]
    assert store.get_source_state("UBI", PRICE_STATE_KEY) is None
    assert store.get_source_state("UBI", "AMF informations réglementées") is None
