"""Interface des fetchers et registre par type de source (cadrage §8.1).

Ajouter un type de source = ajouter un module qui appelle `register(...)`, sans toucher au reste.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from watcher.config import AgentConfig, Source
from watcher.models import NewsItem

log = logging.getLogger(__name__)


class Fetcher(Protocol):
    source_type: str

    def validate_params(self, params: dict[str, Any]) -> None:
        """Lève ValueError si les paramètres de la source sont invalides (appelé au chargement de la config)."""

    def fetch(self, source: Source, agent: AgentConfig, since: datetime) -> list[NewsItem]:
        """Retourne les documents publiés depuis `since` (sans le texte complet)."""

    def fetch_text(self, item: NewsItem) -> str:
        """Récupère le texte complet d'un document retenu au tri."""


_REGISTRY: dict[str, Fetcher] = {}


def register(fetcher: Fetcher) -> Fetcher:
    if fetcher.source_type in _REGISTRY:
        raise ValueError(f"fetcher déjà enregistré pour le type {fetcher.source_type!r}")
    _REGISTRY[fetcher.source_type] = fetcher
    return fetcher


def get_fetcher(source_type: str) -> Fetcher | None:
    return _REGISTRY.get(source_type)


def validate_source_params(source: Source) -> None:
    """Valide les paramètres d'une source active auprès de son fetcher.

    Un type sans fetcher enregistré n'est pas une erreur de config : la source sera sautée au fetch.
    """
    fetcher = get_fetcher(source.type)
    if fetcher is None:
        log.debug("aucun fetcher enregistré pour %s (%s)", source.name, source.type)
        return
    fetcher.validate_params(source.params)
