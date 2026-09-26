from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from watcher.config import Source
from watcher.sources.base import SourceState, item_id
from watcher.sources.edgar import ARCHIVE_URL, SUBMISSIONS_URL, EdgarFetcher, pick_exhibit, split_exhibit
from watcher.sources.http import FetchError
from tests.conftest import OFFLINE, Router, data_file, repo_agent

CIK = 1760854
UA = "watcher-test contact@example.com"
SOURCE = Source(name="SEC EDGAR", type="edgar", primary=True, params={"cik": "1760854", "forms": ["6-K", "20-F"]})
SINCE = datetime(2026, 9, 23, tzinfo=UTC)       # deux 6-K le 24/09 : un communiqué, un rapport XBRL sans exhibit
PRESS = "0001171843-26-006217"
XBRL = "0001760854-26-000006"


def _base(accession: str) -> str:
    return ARCHIVE_URL.format(cik=CIK, accession=accession.replace("-", ""))


def _router() -> Router:
    xbrl_listing = {"directory": {"item": [{"name": f"{XBRL}-index.html"}, {"name": "nbx-20260630.htm"}]}}
    return Router({
        SUBMISSIONS_URL.format(cik=CIK): httpx.Response(200, content=data_file("edgar_submissions_CIK0001760854.json")),
        _base(PRESS) + "index.json": httpx.Response(200, content=data_file(f"edgar_index_{PRESS}.json")),
        _base(PRESS) + "exh_991.htm": httpx.Response(200, content=data_file(f"edgar_exh991_{PRESS}.htm")),
        _base(XBRL) + "index.json": httpx.Response(200, json=xbrl_listing),
    })


def test_fetch_recorded_filings() -> None:
    router = _router()
    items = EdgarFetcher(router.client(), UA).fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())

    assert len(items) == 1                               # le rapport XBRL sans exhibit 99 est écarté
    item = items[0]
    assert item.id == item_id("edgar", PRESS)
    assert item.title == "NANOBIOTIX Provides First Half 2026 Operational and Financial Update"
    assert str(item.url) == _base(PRESS) + "exh_991.htm"
    assert item.published_at == datetime(2026, 9, 24, 20, 15, 3, tzinfo=UTC)
    assert (item.source_primary, item.source_type) == (True, "edgar")
    assert item.summary.startswith("NANOBIOTIX Provides First Half 2026")
    assert len(item.summary) <= 500
    assert all(r.headers["User-Agent"] == UA for r in router.requests)
    assert not any(r.url.path.endswith("nbx-20260630.htm") for r in router.requests)   # 1,7 Mo jamais téléchargés


def test_fetch_text_reuses_downloaded_exhibit() -> None:
    router = _router()
    fetcher = EdgarFetcher(router.client(), UA)
    item = fetcher.fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())[0]
    calls = len(router.requests)
    text = fetcher.fetch_text(item)
    assert len(router.requests) == calls
    assert "110.9 million in cash" in text.replace("\n", " ")
    assert not text.startswith("EX-99.1")

    fresh = EdgarFetcher(router.client(), UA)             # autre run : l'exhibit est retéléchargé
    assert fresh.fetch_text(item) == text


def test_forms_filter() -> None:
    router = _router()
    source = SOURCE.model_copy(update={"params": {"cik": "1760854", "forms": ["20-F"]}})
    assert EdgarFetcher(router.client(), UA).fetch(source, repo_agent("nano"), SINCE, SourceState()) == []
    assert len(router.requests) == 1


def test_missing_user_agent_fails_the_source() -> None:
    with pytest.raises(FetchError, match="SEC_USER_AGENT"):
        EdgarFetcher(_router().client(), None).fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())


def test_unexpected_payload_raises() -> None:
    router = Router({SUBMISSIONS_URL.format(cik=CIK): httpx.Response(200, json={"filings": {}})})
    with pytest.raises(FetchError, match="inattendue"):
        EdgarFetcher(router.client(), UA).fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())


@pytest.mark.parametrize("names, expected", [
    (["f6k_092426.htm", "exh_991.htm"], "exh_991.htm"),
    (["d123dex991.htm", "d123dex992.htm", "d123d6k.htm"], "d123dex991.htm"),
    (["ex-99_1.htm"], "ex-99_1.htm"),
    (["EX99-1.HTM"], "EX99-1.HTM"),
    (["nbx-20260630.htm", "exh_991.jpg"], None),
])
def test_pick_exhibit(names, expected) -> None:
    assert pick_exhibit(names) == expected


def test_split_exhibit_without_header() -> None:
    assert split_exhibit("Titre\nCorps") == (None, "Titre\nCorps")


@pytest.mark.parametrize("params", [
    {"forms": ["6-K"]},
    {"cik": "abc", "forms": ["6-K"]},
    {"cik": True, "forms": ["6-K"]},
    {"cik": "1760854", "forms": []},
    {"cik": "1760854", "forms": "6-K"},
    {"cik": "1760854", "forms": ["6-K"], "url": "x"},
])
def test_validate_params(params) -> None:
    with pytest.raises(ValueError):
        EdgarFetcher(OFFLINE, UA).validate_params(params)


def test_repo_config_and_int_cik_are_valid() -> None:
    fetcher = EdgarFetcher(OFFLINE, UA)
    fetcher.validate_params(SOURCE.params)
    fetcher.validate_params({"cik": 1760854, "forms": ["6-K"]})
