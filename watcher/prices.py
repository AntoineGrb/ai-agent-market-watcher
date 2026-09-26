"""Cours de clôture (cadrage §8.3, docs/sources.md §6).

- `YFinanceProvider` : principal, toutes lignes (Euronext et US).
- `EuronextProvider` : CSV Euronext, repli pour les lignes Euronext Paris, utilisé si yfinance échoue ou si sa
  série présente une séance manquante récente (cas du 24/09/2026).
- Toute barre datée d'aujourd'hui ou après est écartée : c'est une séance en cours (volume partiel).

Écart assumé avec la signature du cadrage : `history` reçoit aussi `today` (date injectée, jamais d'horloge en
dur), nécessaire pour écarter la barre du jour et borner la requête Euronext.

`is_new_close` compare la dernière clôture à la « dernière clôture traitée » gardée dans `source_state` sous la
clé `PRICE_STATE_KEY` ; l'appelant l'écrit dans la transaction de l'agent.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import httpx

from watcher.config import Position
from watcher.engine.metrics import daily_move_pct
from watcher.models import PriceSnapshot
from watcher.sources.http import FetchError, request

log = logging.getLogger(__name__)

PRICE_STATE_KEY = "__price__"      # source_state.source_name réservé au cours (aucune source ne peut le porter)
SESSIONS = 15                      # ~3 semaines de séances (cadrage §3.1)
PRICE_DECIMALS = 6
EURONEXT_PARIS = "Euronext Paris"
EURONEXT_CSV_URL = "https://live.euronext.com/en/ajax/AwlHistoricalPrice/getFullDownloadAjax/{isin}-XPAR"

Closes = list[tuple[date, float]]


class PriceError(Exception):
    """Cours indisponible (provider en échec, série vide ou illisible)."""


class PriceProvider(Protocol):
    name: str

    def history(self, position: Position, sessions: int, *, today: date) -> Closes:
        """Clôtures des `sessions` dernières séances strictement antérieures à `today`, par date croissante."""


def _last_sessions(closes: Closes, sessions: int, today: date) -> Closes:
    kept = sorted((d, c) for d, c in closes if d < today and c > 0)
    return kept[-sessions:]


# --------------------------------------------------------------------------- calendrier Euronext Paris


def _easter(year: int) -> date:
    """Dimanche de Pâques (calendrier grégorien, algorithme de Meeus / Jones / Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def euronext_holidays(year: int) -> set[date]:
    """Jours de fermeture d'Euronext Paris : 1er janvier, Vendredi saint, lundi de Pâques, 1er mai, 25 et 26 décembre."""
    easter = _easter(year)
    return {date(year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1), date(year, 5, 1),
            date(year, 12, 25), date(year, 12, 26)}


def is_euronext_session(day: date) -> bool:
    return day.weekday() < 5 and day not in euronext_holidays(day.year)


def previous_euronext_session(day: date) -> date:
    """Dernière séance strictement antérieure à `day`."""
    candidate = day - timedelta(days=1)
    while not is_euronext_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def missing_recent_session(closes: Closes, today: date) -> str | None:
    """Raison pour laquelle la série Euronext Paris est incomplète sur ses deux dernières séances, sinon None.

    La dernière clôture doit être celle de la veille de bourse, et l'avant-dernière la séance qui la précède :
    sinon la variation J-1 couvrirait en réalité plusieurs séances.
    """
    if not closes:
        return "série vide"
    expected_last = previous_euronext_session(today)
    last = closes[-1][0]
    if last < expected_last:
        return f"dernière clôture au {last.isoformat()}, séance du {expected_last.isoformat()} absente"
    if len(closes) >= 2 and closes[-2][0] < previous_euronext_session(last):
        return f"séance du {previous_euronext_session(last).isoformat()} absente"
    return None


# --------------------------------------------------------------------------- providers


TickerFactory = Callable[[str], Any]


def _yfinance_ticker(symbol: str) -> Any:
    import yfinance   # import local : lourd, et inutile quand le cours n'est pas demandé (tests, baseline sans cours)

    return yfinance.Ticker(symbol)


class YFinanceProvider:
    name = "yfinance"

    def __init__(self, ticker_factory: TickerFactory = _yfinance_ticker) -> None:
        self._ticker = ticker_factory

    def history(self, position: Position, sessions: int, *, today: date) -> Closes:
        try:
            frame = self._ticker(position.price_symbol).history(period="3mo", interval="1d", auto_adjust=False)
        except Exception as exc:   # yfinance lève des types variés (réseau, parsing, symbole inconnu)
            raise PriceError(f"yfinance {position.price_symbol} : {type(exc).__name__} : {exc}") from exc
        if frame is None or getattr(frame, "empty", True) or "Close" not in frame:
            raise PriceError(f"yfinance {position.price_symbol} : série vide")
        # NaN exclus ; arrondi : yfinance stocke en float32 (29.16 devient 29.15999984741211).
        closes = [(ts.date(), round(float(value), PRICE_DECIMALS)) for ts, value in frame["Close"].items()
                  if value == value]
        kept = _last_sessions(closes, sessions, today)
        if not kept:
            raise PriceError(f"yfinance {position.price_symbol} : aucune clôture antérieure au {today.isoformat()}")
        return kept


class EuronextProvider:
    name = "euronext"

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def history(self, position: Position, sessions: int, *, today: date) -> Closes:
        start = today - timedelta(days=sessions * 2 + 14)   # marge pour week-ends et jours fériés
        params = {
            "format": "csv", "decimal_separator": ".", "date_form": "d/m/Y", "op": "", "adjusted": "Y",
            "base100": "", "startdate": start.isoformat(), "enddate": today.isoformat(),
        }
        try:
            response = request(self._client, "GET", EURONEXT_CSV_URL.format(isin=position.isin), params=params)
        except FetchError as exc:
            raise PriceError(f"Euronext {position.isin} : {exc}") from exc
        kept = _last_sessions(parse_euronext_csv(response.content.decode("utf-8-sig")), sessions, today)
        if not kept:
            raise PriceError(f"Euronext {position.isin} : aucune clôture antérieure au {today.isoformat()}")
        return kept


def parse_euronext_csv(text: str) -> Closes:
    """CSV `;` avec 3 lignes d'en-tête puis `Date;Open;High;Low;Last;Close;...`, dates `JJ/MM/AAAA` décroissantes."""
    lines = text.lstrip("﻿").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("Date;"))
    except StopIteration as exc:
        raise PriceError("CSV Euronext : ligne d'en-tête 'Date;...' introuvable") from exc
    closes: Closes = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=";"):
        try:
            closes.append((datetime.strptime(row["Date"], "%d/%m/%Y").date(), float(row["Close"])))
        except (KeyError, TypeError, ValueError):
            log.debug("ligne CSV Euronext ignorée : %r", row)
    return closes


# --------------------------------------------------------------------------- instantané


@dataclass(frozen=True)
class PriceFetch:
    closes: Closes
    provider: str
    warnings: tuple[str, ...] = ()


class PriceService:
    """Choisit le provider (principal, repli Euronext Paris) et construit le `PriceSnapshot`."""

    def __init__(self, primary: PriceProvider, euronext_fallback: PriceProvider | None = None) -> None:
        self.primary = primary
        self.fallback = euronext_fallback

    def closes(self, position: Position, *, today: date, sessions: int = SESSIONS) -> PriceFetch:
        warnings: list[str] = []
        closes: Closes = []
        try:
            closes = self.primary.history(position, sessions, today=today)
        except PriceError as exc:
            warnings.append(str(exc))

        use_fallback = self.fallback is not None and position.listing == EURONEXT_PARIS
        if use_fallback:
            gap = missing_recent_session(closes, today) if closes else None
            if closes and gap is None:
                return PriceFetch(closes, self.primary.name, tuple(warnings))
            if gap is not None:
                warnings.append(f"{self.primary.name} {position.price_symbol} : {gap}")
            try:
                fallback_closes = self.fallback.history(position, sessions, today=today)
            except PriceError as exc:
                warnings.append(str(exc))
            else:
                warnings.append(f"repli {self.fallback.name} utilisé pour {position.ticker}")
                return PriceFetch(fallback_closes, self.fallback.name, tuple(warnings))

        if not closes:
            raise PriceError(" ; ".join(warnings) or f"aucun cours pour {position.ticker}")
        return PriceFetch(closes, self.primary.name, tuple(warnings))


def build_snapshot(position: Position, closes: Closes, last_processed: date | None) -> PriceSnapshot:
    """`is_new_close` : la dernière clôture est postérieure à la dernière déjà traitée (ou aucune ne l'a été)."""
    if not closes:
        raise PriceError(f"aucun cours pour {position.ticker}")
    last_date, last = closes[-1]
    prev = closes[-2][1] if len(closes) >= 2 else None
    return PriceSnapshot(
        symbol=position.price_symbol,
        last_close=last,
        last_close_date=last_date,
        prev_close=prev,
        daily_move_pct=daily_move_pct(prev, last),
        is_new_close=last_processed is None or last_date > last_processed,
        history=closes,
    )
