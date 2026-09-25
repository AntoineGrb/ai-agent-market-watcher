from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from watcher.config import Action, Severity
from watcher.models import Alert, NewsItem
from watcher.store import SCHEMA_VERSION, RunTotals, Store, StoreError, ts
from tests.conftest import NOW


def _item(item_id: str, agent_id: str = "NANO", published_at: datetime | None = NOW) -> NewsItem:
    return NewsItem(id=item_id, agent_id=agent_id, source_name="SEC EDGAR", source_type="edgar",
                    source_primary=True, url=f"https://example.com/{item_id}", title=f"Doc {item_id}",
                    published_at=published_at)


def _alert(rule_id: str = "N-S1") -> Alert:
    return Alert(agent_id="NANO", rule_id=rule_id, source_rule_id=rule_id, origin="event",
                 action=Action.RECO_SELL_ALL, severity=Severity.CRITICAL, headline="h", rationale="r",
                 event_date=date(2026, 9, 24), confidence=0.9)


def test_schema_created_and_migration_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "watcher.sqlite"
    with Store.open(path) as store:
        assert store.schema_version() == SCHEMA_VERSION
    with Store.open(path) as store:   # réouverture : aucune migration rejouée
        assert store.schema_version() == SCHEMA_VERSION
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"seen_items", "events", "armed_watches", "source_state", "runs", "config_errors",
            "schema_version"} <= tables


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "w.sqlite"
    Store.open(path).close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION + 1,))
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="plus récente que le code"):
        Store.open(path)


def test_ts_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="sans fuseau"):
        ts(datetime(2026, 9, 25))
    assert ts(NOW) == "2026-09-25T05:00:00+00:00"


def test_transaction_rolls_back_on_error(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.mark_seen([_item("a")], NOW)
            store.set_source_state("NANO", "ClinicalTrials.gov", {"status": "RECRUITING"}, NOW)
            raise RuntimeError("agent en échec")
    assert store.seen_item_ids("NANO", ["a"]) == set()
    assert store.get_source_state("NANO", "ClinicalTrials.gov") is None

    with store.transaction():
        store.mark_seen([_item("a")], NOW)
    assert store.seen_item_ids("NANO", ["a"]) == {"a"}


def test_nested_transactions_refused(store: Store) -> None:
    with store.transaction():
        with pytest.raises(StoreError, match="imbriquées"):
            with store.transaction():
                pass


def test_runs(store: Store) -> None:
    run_id = store.start_run("test", NOW)
    assert store.get_run(run_id)["status"] == "running"
    store.finish_run(run_id, NOW + timedelta(minutes=2),
                     RunTotals(status="partial", agents_ok=1, agents_failed=1, errors={"NANO": "config"}))
    row = store.get_run(run_id)
    assert (row["status"], row["agents_ok"], row["agents_failed"]) == ("partial", 1, 1)
    assert row["finished_at"] == "2026-09-25T05:02:00+00:00"
    assert '"NANO": "config"' in row["errors_json"]


def test_config_error_lifecycle(store: Store) -> None:
    later = NOW + timedelta(days=1)
    assert store.record_config_error("NANO", "h1", "msg", NOW) is True
    assert store.record_config_error("NANO", "h1", "msg", later) is True      # pas encore notifiée
    store.mark_config_error_notified("NANO", "h1", NOW)
    assert store.record_config_error("NANO", "h1", "msg", later) is False     # déjà notifiée
    assert [e.error_hash for e in store.open_config_errors()] == ["h1"]

    assert store.resolve_config_errors("NANO", later, keep=["h1"]) == 0
    assert store.resolve_config_errors("NANO", later) == 1
    assert store.open_config_errors() == []

    assert store.record_config_error("NANO", "h1", "msg", later) is True      # régression : renotifiée
    assert store.open_config_errors()[0].notified_at is None


def test_seen_items(store: Store) -> None:
    store.mark_seen([_item("a"), _item("b", published_at=None)], NOW, relevant={"a"})
    store.mark_seen([_item("a")], NOW)                                        # doublon ignoré
    assert store.seen_item_ids("NANO", ["a", "b", "c"]) == {"a", "b"}
    assert store.seen_item_ids("UBI", ["a"]) == set()
    ids = [f"x{i}" for i in range(1200)]                                     # au-delà de la limite de variables
    store.mark_seen([_item(i) for i in ids], NOW)
    assert len(store.seen_item_ids("NANO", ids)) == 1200


def test_mark_seen_rejects_naive_published_at(store: Store) -> None:
    with pytest.raises(ValueError):
        store.mark_seen([_item("a", published_at=datetime(2026, 9, 24))], NOW)


def test_source_state_upsert(store: Store) -> None:
    store.set_source_state("NANO", "ClinicalTrials.gov", {"status": "RECRUITING"}, NOW)
    store.set_source_state("NANO", "ClinicalTrials.gov", {"status": "COMPLETED"}, NOW)
    assert store.get_source_state("NANO", "ClinicalTrials.gov") == {"status": "COMPLETED"}


def test_events_outbox(store: Store) -> None:
    first = store.insert_event(_alert("N-S1"), "N-S1", NOW)
    second = store.insert_event(_alert("N-B1"), "N-B1", NOW)
    pending = store.pending_events()
    assert [e.id for e in pending] == [first, second]
    assert pending[0].alert == _alert("N-S1")
    assert store.fired_rule_ids("NANO") == {"N-S1", "N-B1"}

    store.mark_sent([first], NOW)
    assert [e.id for e in store.pending_events()] == [second]   # la non-envoyée reste dans l'outbox
