from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from watcher.config import Source
from watcher.sources.base import SourceState, item_id
from watcher.sources.dila_amf import API_URL, PAGE_SIZE, DilaAmfFetcher
from watcher.sources.http import FetchError
from tests.conftest import NOW, OFFLINE, Router, data_file, repo_agent

ISIN = "FR0011341205"
SOURCE = Source(name="AMF informations réglementées", type="dila_amf", primary=True, params={"isin": ISIN})
SINCE = NOW - timedelta(days=3)          # 22/09 05:00 UTC
RECORDS = json.loads(data_file(f"amf_records_{ISIN}.json"))
PDF_URL = "https://fr.ftp.opendatasoft.com/datadila/INFOFI/MKW/2026/09/FCMKW119823_20260922.pdf"


def _fetcher(router: Router) -> DilaAmfFetcher:
    return DilaAmfFetcher(router.client(), clock=lambda: NOW)


def test_fetch_recorded_records() -> None:
    router = Router({API_URL: httpx.Response(200, json=RECORDS)})
    items = _fetcher(router).fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())

    params = router.requests[0].url.params
    assert params["where"] == (f'identificationsociete_iso_cd_isi="{ISIN}" '
                               "and informationdeposee_inf_dat_emt>=date'2026-09-22'")
    assert params["order_by"] == "informationdeposee_inf_dat_emt desc"
    assert params["limit"] == str(PAGE_SIZE)

    assert len(items) == 5                            # 20 enregistrements, 5 depuis le 22/09 05:00 UTC
    by_title = {it.title: it for it in items}
    press = by_title["NANOBIOTIX Provides First Half 2026 Operational and Financial Update"]
    assert press.id == item_id("dila_amf", "113617_20260924")
    assert press.published_at == datetime(2026, 9, 24, 20, 15, tzinfo=UTC)
    assert str(press.url) == "https://fr.ftp.opendatasoft.com/datadila/INFOFI/MKW/2026/09/FCMKW113617_20260924.pdf"
    assert "document déposé par l'émetteur" in press.summary and "langue : Anglais" in press.summary
    assert (press.source_primary, press.source_type, press.agent_id) == (True, "dila_amf", "NANO")


def test_threshold_crossings_are_kept_and_labelled() -> None:
    router = Router({API_URL: httpx.Response(200, json=RECORDS)})
    since = datetime(2026, 9, 21, tzinfo=UTC)
    items = _fetcher(router).fetch(SOURCE, repo_agent("nano"), since, SourceState())
    crossings = [it for it in items if it.title.startswith("Franchissement")]
    assert len(crossings) == 2
    assert all("publication de l'AMF" in it.summary for it in crossings)


def test_aberrant_dates_and_other_isins_are_dropped() -> None:
    good = RECORDS["results"][0]
    future = {**good, "uin_idt_uin": "x_future", "informationdeposee_inf_dat_emt": "5015-02-04T00:00:00+00:00"}
    other = {**good, "uin_idt_uin": "x_other", "identificationsociete_iso_cd_isi": "FR0000054470"}
    broken = {**good, "uin_idt_uin": None}
    router = Router({API_URL: httpx.Response(200, json={"total_count": 4, "results": [good, future, other, broken]})})
    items = _fetcher(router).fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())
    assert [it.id for it in items] == [item_id("dila_amf", good["uin_idt_uin"])]


def test_pagination() -> None:
    record = RECORDS["results"][0]
    page = [{**record, "uin_idt_uin": f"id_{i}"} for i in range(PAGE_SIZE)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"results": page if offset == 0 else page[:3]})

    router = Router({API_URL: handler})
    items = _fetcher(router).fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())
    assert [r.url.params["offset"] for r in router.requests] == ["0", "100"]
    assert len(items) == PAGE_SIZE + 3  # arrêt dès qu'une page est incomplète


def test_unexpected_payload_raises() -> None:
    router = Router({API_URL: httpx.Response(200, json={"error_code": "ODSQLError"})})
    with pytest.raises(FetchError, match="inattendue"):
        _fetcher(router).fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())


def test_fetch_text_extracts_recorded_pdf() -> None:
    router = Router({
        API_URL: httpx.Response(200, json=RECORDS),
        PDF_URL: httpx.Response(200, content=data_file("amf_nano_voting_rights.pdf")),
    })
    fetcher = _fetcher(router)
    item = next(it for it in fetcher.fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())
                if str(it.url) == PDF_URL)
    text = fetcher.fetch_text(item)
    assert "VOTING RIGHTS AND SHARES CAPITAL OF THE COMPANY" in text
    assert "September 22, 2026" in text


def test_fetch_text_of_empty_pdf_falls_back_to_title(monkeypatch: pytest.MonkeyPatch) -> None:
    from watcher.sources import dila_amf

    monkeypatch.setattr(dila_amf, "pdf_to_text", lambda data: "")
    router = Router({API_URL: httpx.Response(200, json=RECORDS), PDF_URL: httpx.Response(200, content=b"%PDF")})
    fetcher = _fetcher(router)
    item = next(it for it in fetcher.fetch(SOURCE, repo_agent("nano"), SINCE, SourceState())
                if str(it.url) == PDF_URL)
    assert "non extractible" in fetcher.fetch_text(item)


@pytest.mark.parametrize("params", [{}, {"isin": "FR001"}, {"isin": ISIN, "base_url": "ftp://x"}])
def test_validate_params(params) -> None:
    with pytest.raises(ValueError):
        DilaAmfFetcher(OFFLINE).validate_params(params)


def test_repo_configs_are_valid() -> None:
    fetcher = DilaAmfFetcher(OFFLINE)
    for agent in ("ubi", "nano"):
        for source in repo_agent(agent).sources:
            if source.type == "dila_amf":
                fetcher.validate_params(source.params)
