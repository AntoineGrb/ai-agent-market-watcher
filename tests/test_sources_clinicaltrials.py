from __future__ import annotations

import copy
import json
from datetime import timedelta

import httpx
import pytest

from watcher.config import Source
from watcher.sources.base import SourceState
from watcher.sources.clinicaltrials import API_URL, ClinicalTrialsFetcher, diff, snapshot
from watcher.sources.http import FetchError
from tests.conftest import NOW, OFFLINE, Router, data_file, repo_agent

NCT = "NCT04892173"
SOURCE = Source(name="ClinicalTrials.gov", type="clinicaltrials", primary=True, params={"nct_id": NCT})
SINCE = NOW - timedelta(days=3)
STUDY = json.loads(data_file(f"clinicaltrials_{NCT}.json"))


def _fetch(study: dict, previous: dict | None) -> tuple[list, SourceState]:
    router = Router({API_URL.format(nct_id=NCT): httpx.Response(200, json=study)})
    state = SourceState(previous=previous)
    items = ClinicalTrialsFetcher(router.client(), clock=lambda: NOW).fetch(SOURCE, repo_agent("nano"), SINCE, state)
    return items, state


def test_snapshot_of_recorded_study() -> None:
    assert snapshot(STUDY) == {
        "overall_status": "RECRUITING",
        "primary_completion_date": "2028-06-30 (ESTIMATED)",
        "completion_date": "2028-06-30 (ESTIMATED)",
        "has_results": False,
        "last_update_posted": "2026-08-28",
        "primary_outcomes": ["Progression-free Survival (PFS) Based on Independent Central Review (ICR)"],
    }


def test_first_run_is_the_reference_without_document() -> None:
    items, state = _fetch(STUDY, previous=None)
    assert items == []
    assert state.updated == {"snapshot": snapshot(STUDY)}


def test_no_change_no_document() -> None:
    items, state = _fetch(STUDY, previous={"snapshot": snapshot(STUDY)})
    assert items == []
    assert state.updated == {"snapshot": snapshot(STUDY)}


def test_change_produces_a_synthetic_document_with_the_diff() -> None:
    changed = copy.deepcopy(STUDY)
    status = changed["protocolSection"]["statusModule"]
    status["overallStatus"] = "ACTIVE_NOT_RECRUITING"
    status["primaryCompletionDateStruct"] = {"date": "2027-06-30", "type": "ESTIMATED"}
    status["lastUpdatePostDateStruct"] = {"date": "2026-09-24", "type": "ACTUAL"}
    changed["hasResults"] = True

    items, state = _fetch(changed, previous={"snapshot": snapshot(STUDY)})

    assert len(items) == 1
    item = items[0]
    assert (item.source_primary, item.source_type, item.agent_id) == (True, "clinicaltrials", "NANO")
    assert str(item.url) == f"https://clinicaltrials.gov/study/{NCT}"
    assert item.published_at == NOW
    assert "statut global" in item.title and "résultats publiés" in item.title
    assert "- Statut global : RECRUITING → ACTIVE_NOT_RECRUITING" in item.text
    assert "2028-06-30 (ESTIMATED) → 2027-06-30 (ESTIMATED)" in item.text
    assert "Résultats publiés dans le registre : non → oui" in item.text
    assert "Critères d'évaluation principaux" not in item.text          # inchangés
    assert state.updated == {"snapshot": snapshot(changed)}
    fetcher = ClinicalTrialsFetcher(OFFLINE)
    assert fetcher.fetch_text(item) == item.text
    # Même changement revu au run suivant (état non commité) : même ID, donc dédoublonné par seen_items.
    assert _fetch(changed, previous={"snapshot": snapshot(STUDY)})[0][0].id == item.id


def test_diff_formats_missing_values() -> None:
    before = {"overall_status": None, "primary_outcomes": []}
    after = {"overall_status": "COMPLETED", "primary_outcomes": ["OS"]}
    assert diff(before, after) == [
        "- Statut global : (non renseigné) → COMPLETED",
        "- Critères d'évaluation principaux : (aucun) → OS",
    ]


def test_unexpected_payload_raises() -> None:
    with pytest.raises(FetchError, match="inattendue"):
        _fetch({"error": "not found"}, previous=None)


@pytest.mark.parametrize("params", [{}, {"nct_id": "04892173"}, {"nct_id": NCT, "x": 1}])
def test_validate_params(params) -> None:
    with pytest.raises(ValueError):
        ClinicalTrialsFetcher(OFFLINE).validate_params(params)


def test_repo_config_is_valid() -> None:
    ClinicalTrialsFetcher(OFFLINE).validate_params(SOURCE.params)
