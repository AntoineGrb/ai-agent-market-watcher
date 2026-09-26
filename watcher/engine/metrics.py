"""Métriques calculées par le code (cadrage §4.3).

Le LLM n'extrait que des chiffres bruts ; tous les ratios, dilutions et primes sont calculés ici. Une métrique
qui ne peut pas être calculée lève `MetricUnavailable` : c'est l'appelant qui décide (RECO_UNCLEAR ou override
ignoré, cadrage §6.1), jamais une valeur par défaut inventée.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date

from watcher.config import Condition, Op, Position
from watcher.models import PriceSnapshot

EXISTING_SHARES_FIGURE = "existing_shares"   # chiffre extrait prioritaire sur position.shares_outstanding
NEW_SHARES_FIGURE = "new_shares"

# Les ratios sont arrondis avant comparaison : 10.66 / 5.33 doit valoir exactement 2, pas 1.9999999999999998.
COMPARISON_DIGITS = 9

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
}


class MetricUnavailable(Exception):
    """Une métrique ne peut pas être calculée (chiffre non extrait, cours manquant...)."""


def compare(value: float, op: Op, threshold: float) -> bool:
    return _OPS[op](round(value, COMPARISON_DIGITS), threshold)


def metric_label(cond: Condition) -> str:
    """Clé de la métrique dans `Alert.metrics` : `dilution_pct`, `figure_vs_prev_close(offer_price)`..."""
    return f"{cond.metric}({cond.figure})" if cond.figure else cond.metric


def daily_move_pct(prev_close: float | None, last_close: float) -> float | None:
    """Variation J-1 en % ; None si la clôture précédente est absente ou inexploitable."""
    if prev_close is None or prev_close <= 0:
        return None
    return 100 * (last_close / prev_close - 1)


def dilution_pct(new_shares: float, existing_shares: float | None) -> float:
    """`100 × new / (existing + new)`. Sans action nouvelle, 0 sans avoir besoin du nombre d'actions existantes."""
    if new_shares < 0:
        raise MetricUnavailable(f"dilution_pct : new_shares négatif ({new_shares:g})")
    if new_shares == 0:
        return 0.0
    if existing_shares is None or existing_shares <= 0:
        raise MetricUnavailable(
            "dilution_pct : nombre d'actions existantes inconnu (ni existing_shares extrait, "
            "ni position.shares_outstanding renseigné)"
        )
    return 100 * new_shares / (existing_shares + new_shares)


@dataclass
class MetricContext:
    """Données disponibles pour évaluer les conditions d'un match ou d'une règle déterministe.

    `computed` accumule les métriques effectivement calculées, pour les afficher dans le mail.
    """

    position: Position
    price: PriceSnapshot | None = None
    figures: Mapping[str, float] = field(default_factory=dict)
    event_date: date | None = None
    computed: dict[str, float] = field(default_factory=dict)

    def check(self, cond: Condition) -> bool:
        value = self.value(cond)
        self.computed[metric_label(cond)] = round(value, 6)
        return compare(value, cond.op, cond.value)

    def value(self, cond: Condition) -> float:
        match cond.metric:
            case "price_vs_entry":
                return self._price_vs_entry()
            case "dilution_pct":
                return self._dilution_pct()
            case "figure_vs_prev_close":
                return self._figure_vs_prev_close(self._required_figure(cond))
            case "price_vs_figure":
                return self._price_vs_figure(self._required_figure(cond))
            case "figure":
                return self._figure(self._required_figure(cond))
        raise MetricUnavailable(f"métrique inconnue : {cond.metric}")   # pragma: no cover - Literal exhaustif

    # ------------------------------------------------------------------ données de base

    @staticmethod
    def _required_figure(cond: Condition) -> str:
        if not cond.figure:   # pragma: no cover - garanti par le validateur de Condition
            raise MetricUnavailable(f"{cond.metric} : aucun chiffre désigné")
        return cond.figure

    def _figure(self, name: str) -> float:
        if name not in self.figures:
            raise MetricUnavailable(f"chiffre {name} non extrait")
        value = float(self.figures[name])
        if not math.isfinite(value):
            raise MetricUnavailable(f"chiffre {name} invalide ({value})")
        return value

    def _last_close(self) -> float:
        if self.price is None:
            raise MetricUnavailable("cours indisponible")
        return self.price.last_close

    # ------------------------------------------------------------------ métriques

    def _price_vs_entry(self) -> float:
        entry = self.position.entry_price
        if self.position.status != "OWNED" or entry is None:
            raise MetricUnavailable("price_vs_entry : aucune position détenue avec un prix d'entrée")
        return self._last_close() / entry

    def _dilution_pct(self) -> float:
        new_shares = self._figure(NEW_SHARES_FIGURE)
        if EXISTING_SHARES_FIGURE in self.figures:
            existing: float | None = self._figure(EXISTING_SHARES_FIGURE)
        else:
            existing = self.position.shares_outstanding
        return dilution_pct(new_shares, existing)

    def _figure_vs_prev_close(self, name: str) -> float:
        figure = self._figure(name)
        if self.event_date is None:
            raise MetricUnavailable(f"figure_vs_prev_close({name}) : date de l'événement inconnue")
        if self.price is None:
            raise MetricUnavailable(f"figure_vs_prev_close({name}) : cours indisponible")
        before = [(d, close) for d, close in self.price.history if d < self.event_date]
        if not before:
            raise MetricUnavailable(
                f"figure_vs_prev_close({name}) : aucune clôture antérieure au {self.event_date.isoformat()}"
            )
        _, prev_close = max(before)
        if prev_close <= 0:
            raise MetricUnavailable(f"figure_vs_prev_close({name}) : clôture de référence invalide")
        return figure / prev_close

    def _price_vs_figure(self, name: str) -> float:
        reference = self._figure(name)
        if reference <= 0:
            raise MetricUnavailable(f"price_vs_figure({name}) : valeur de référence invalide ({reference:g})")
        return self._last_close() / reference
