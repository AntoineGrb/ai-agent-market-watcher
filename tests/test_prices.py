from __future__ import annotations

from datetime import date

import httpx
import pandas as pd
import pytest

from watcher.prices import (
    EURONEXT_CSV_URL,
    EuronextProvider,
    PriceError,
    PriceService,
    YFinanceProvider,
    build_snapshot,
    euronext_holidays,
    missing_recent_session,
    parse_euronext_csv,
    previous_euronext_session,
)
from tests.conftest import TODAY, Router, data_file, repo_agent

UBI = repo_agent("ubi").position
CSV = data_file("euronext_FR0000054470-XPAR.csv")


# --------------------------------------------------------------------------- calendrier


def test_euronext_holidays_2026() -> None:
    assert euronext_holidays(2026) == {
        date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1), date(2026, 12, 25), date(2026, 12, 26),
    }


@pytest.mark.parametrize("day, expected", [
    (date(2026, 9, 25), date(2026, 9, 24)),     # vendredi → jeudi
    (date(2026, 9, 28), date(2026, 9, 25)),     # lundi → vendredi
    (date(2026, 9, 27), date(2026, 9, 25)),     # dimanche → vendredi
    (date(2026, 4, 7), date(2026, 4, 2)),       # mardi après Pâques → jeudi saint
    (date(2027, 1, 4), date(2026, 12, 31)),     # lundi 4 janvier → 31 décembre
])
def test_previous_session(day: date, expected: date) -> None:
    assert previous_euronext_session(day) == expected


def test_missing_recent_session() -> None:
    complete = [(date(2026, 9, 22), 5.4), (date(2026, 9, 23), 5.43), (date(2026, 9, 24), 5.40)]
    assert missing_recent_session(complete, TODAY) is None
    # Cas réel du 24/09/2026 : yfinance n'avait pas la séance du 24.
    assert "24" in missing_recent_session(complete[:2], TODAY)
    # Trou entre les deux dernières séances : la variation J-1 couvrirait deux séances.
    assert "2026-09-23" in missing_recent_session([complete[0], complete[2]], TODAY)
    assert missing_recent_session([], TODAY) == "série vide"
    assert missing_recent_session(complete[2:], TODAY) is None     # une seule séance : rien à comparer


# --------------------------------------------------------------------------- Euronext


def test_parse_recorded_csv() -> None:
    closes = parse_euronext_csv(CSV.decode("utf-8-sig"))
    assert closes[0] == (date(2026, 9, 24), 5.40)
    assert (date(2026, 9, 23), 5.432) in closes
    with pytest.raises(PriceError, match="en-tête"):
        parse_euronext_csv("pas un csv")


def test_euronext_provider() -> None:
    router = Router({EURONEXT_CSV_URL.format(isin=UBI.isin): httpx.Response(200, content=CSV)})
    closes = EuronextProvider(router.client()).history(UBI, 3, today=TODAY)
    assert closes == [(date(2026, 9, 22), 5.412), (date(2026, 9, 23), 5.432), (date(2026, 9, 24), 5.40)]
    params = router.requests[0].url.params
    assert (params["enddate"], params["startdate"]) == ("2026-09-25", "2026-09-05")


def test_euronext_provider_errors() -> None:
    router = Router({EURONEXT_CSV_URL.format(isin=UBI.isin): httpx.Response(500)})
    with pytest.raises(PriceError, match="Euronext"):
        EuronextProvider(router.client()).history(UBI, 3, today=TODAY)
    with pytest.raises(PriceError, match="aucune clôture"):
        empty = Router({EURONEXT_CSV_URL.format(isin=UBI.isin): httpx.Response(200, content=CSV)})
        EuronextProvider(empty.client()).history(UBI, 3, today=date(2026, 9, 1))


# --------------------------------------------------------------------------- yfinance


class FakeTicker:
    def __init__(self, frame: pd.DataFrame | Exception) -> None:
        self.frame = frame
        self.calls: list[dict] = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.frame, Exception):
            raise self.frame
        return self.frame


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="Europe/Paris") for d, _ in rows], name="Date")
    return pd.DataFrame({"Close": [c for _, c in rows], "Volume": [1000] * len(rows)}, index=index)


def test_yfinance_drops_todays_partial_bar_and_nan() -> None:
    frame = _frame([("2026-09-22", 5.41), ("2026-09-23", float("nan")), ("2026-09-24", 5.40), ("2026-09-25", 5.9)])
    ticker = FakeTicker(frame)
    closes = YFinanceProvider(lambda symbol: ticker).history(UBI, 15, today=TODAY)
    assert closes == [(date(2026, 9, 22), 5.41), (date(2026, 9, 24), 5.40)]
    assert ticker.calls == [{"period": "3mo", "interval": "1d", "auto_adjust": False}]


def test_yfinance_float32_noise_is_rounded() -> None:
    frame = _frame([("2026-09-24", 29.15999984741211)])
    assert YFinanceProvider(lambda s: FakeTicker(frame)).history(UBI, 15, today=TODAY) == [(date(2026, 9, 24), 29.16)]


def test_yfinance_keeps_last_sessions_only() -> None:
    frame = _frame([("2026-09-21", 1.0), ("2026-09-22", 2.0), ("2026-09-23", 3.0), ("2026-09-24", 4.0)])
    closes = YFinanceProvider(lambda s: FakeTicker(frame)).history(UBI, 2, today=TODAY)
    assert closes == [(date(2026, 9, 23), 3.0), (date(2026, 9, 24), 4.0)]


@pytest.mark.parametrize("frame, message", [
    (RuntimeError("TLS"), "RuntimeError : TLS"),
    (pd.DataFrame(), "série vide"),
    (_frame([("2026-09-25", 5.9)]), "aucune clôture"),
])
def test_yfinance_errors(frame, message) -> None:
    with pytest.raises(PriceError, match=message):
        YFinanceProvider(lambda s: FakeTicker(frame)).history(UBI, 15, today=TODAY)


# --------------------------------------------------------------------------- choix du provider


class StubProvider:
    def __init__(self, name: str, result: list | Exception) -> None:
        self.name = name
        self.result = result
        self.calls = 0

    def history(self, position, sessions, *, today):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


COMPLETE = [(date(2026, 9, 22), 5.41), (date(2026, 9, 23), 5.43), (date(2026, 9, 24), 5.40)]
GAPPED = COMPLETE[:2]


def test_primary_used_when_complete() -> None:
    fallback = StubProvider("euronext", COMPLETE)
    fetched = PriceService(StubProvider("yfinance", COMPLETE), fallback).closes(UBI, today=TODAY)
    assert (fetched.provider, fetched.warnings, fallback.calls) == ("yfinance", (), 0)


def test_fallback_on_missing_session() -> None:
    fetched = PriceService(StubProvider("yfinance", GAPPED), StubProvider("euronext", COMPLETE)).closes(UBI, today=TODAY)
    assert fetched.provider == "euronext" and fetched.closes == COMPLETE
    # un seul message, sans date : regroupé (×N) dans le heartbeat d'un run à l'autre
    assert fetched.warnings == ("repli euronext utilisé pour UBI (yfinance sans la dernière séance)",)


def test_fallback_on_primary_failure() -> None:
    service = PriceService(StubProvider("yfinance", PriceError("yfinance KO")), StubProvider("euronext", COMPLETE))
    fetched = service.closes(UBI, today=TODAY)
    assert fetched.provider == "euronext"
    assert fetched.warnings == ("repli euronext utilisé pour UBI (yfinance en échec : yfinance KO)",)


def test_gapped_primary_kept_when_fallback_fails() -> None:
    service = PriceService(StubProvider("yfinance", GAPPED), StubProvider("euronext", PriceError("Euronext KO")))
    fetched = service.closes(UBI, today=TODAY)
    assert fetched.provider == "yfinance" and fetched.closes == GAPPED
    assert "Euronext KO" in fetched.warnings


def test_both_providers_fail() -> None:
    service = PriceService(StubProvider("yfinance", PriceError("A")), StubProvider("euronext", PriceError("B")))
    with pytest.raises(PriceError, match="A ; B"):
        service.closes(UBI, today=TODAY)


def test_no_fallback_outside_euronext_paris() -> None:
    us_line = UBI.model_copy(update={"listing": "Nasdaq", "price_symbol": "TTWO"})
    fallback = StubProvider("euronext", COMPLETE)
    fetched = PriceService(StubProvider("yfinance", GAPPED), fallback).closes(us_line, today=TODAY)
    assert (fetched.provider, fallback.calls) == ("yfinance", 0)
    with pytest.raises(PriceError, match="KO"):
        PriceService(StubProvider("yfinance", PriceError("KO")), fallback).closes(us_line, today=TODAY)


# --------------------------------------------------------------------------- instantané


def test_build_snapshot() -> None:
    snap = build_snapshot(UBI, COMPLETE, last_processed=None)
    assert (snap.symbol, snap.last_close, snap.last_close_date, snap.prev_close) == (
        "UBI.PA", 5.40, date(2026, 9, 24), 5.43)
    assert snap.daily_move_pct == pytest.approx(100 * (5.40 / 5.43 - 1))
    assert snap.is_new_close and snap.history == COMPLETE
    assert build_snapshot(UBI, COMPLETE, last_processed=date(2026, 9, 23)).is_new_close
    assert not build_snapshot(UBI, COMPLETE, last_processed=date(2026, 9, 24)).is_new_close


def test_build_snapshot_single_close_and_empty() -> None:
    snap = build_snapshot(UBI, COMPLETE[-1:], last_processed=None)
    assert (snap.prev_close, snap.daily_move_pct) == (None, None)
    with pytest.raises(PriceError):
        build_snapshot(UBI, [], last_processed=None)
